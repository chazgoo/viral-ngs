'''
    Tool wrapper for SPAdes, St. Petersburg Assembler ( http://cab.spbu.ru/software/spades/ )
'''

import logging
import os
import os.path
import subprocess
import shutil
import random
import shlex
import tempfile
import collections

import Bio.SeqIO

import tools
import tools.samtools
import tools.picard
import util.file
import util.misc

TOOL_NAME = 'spades'
TOOL_VERSION = '3.13.0'

log = logging.getLogger(__name__)

class SpadesTool(tools.Tool):
    '''Tool wrapper for SPAdes tool (St. Petersburg Assembler)'''

    def __init__(self, install_methods=None):
        if install_methods is None:
            install_methods = [tools.CondaPackage(TOOL_NAME, version=TOOL_VERSION, executable='spades.py',
                                                  verifycmd='spades.py --version')]
        tools.Tool.__init__(self, install_methods=install_methods)

    def version(self):
        return TOOL_VERSION

    def execute(self, args):    # pylint: disable=W0221
        tool_cmd = [self.install_and_get_path()] + list(map(str, args))
        log.debug(' '.join(tool_cmd))
        subprocess.check_call(tool_cmd)

    Library = collections.namedtuple('Library', 'reads_fwd reads_bwd reads_unpaired')

    def assemble(self, libraries, contigs_out, contigs_trusted=None,
                 contigs_untrusted=None, kmer_sizes=None, mode='rna', always_succeed=False, max_kmer_sizes=1, 
                 filter_contigs=False, min_contig_len=0, mem_limit_gb=8, threads=None, spades_opts=''):
        '''Assemble contigs from RNA-seq reads and (optionally) pre-existing contigs.

        Inputs:
            Required:
              libraries: instances of Library (fasta/q): paired reads from libraries, with single reads optional
            Optional:
              contigs_trusted (fasta/q): optionally, already-assembled contigs of high quality
              contigs_untrusted (fasta/q): optionally, already-assembled contigs of average quality
        Params:
            kmer_sizes: if given, use these kmer sizes and combine the resulting contigs.  kmer size of 0 or None
              means use size auto-selected by SPAdes based on read length.
            mode: 'rna' for rnaSPAdes or 'meta' for metaSPAdes
            always_succeed: if True, if spades fails with an error for a kmer size, pretend it just produced no 
              contigs for that kmer size
            max_kmer_sizes: if this many kmer sizes succeed, do not try further ones
            filter_contigs: if True, outputs only "long and reliable transcripts with rather high expression" (rna mode)
            min_contig_len: drop contigs shorter than this many bp
            mem_limit_gb: max memory to use, in gigabytes
            threads: number of threads to use
            spades_opts: additional options to pass to spades
        Outputs:
            contigs_out: assembled contigs in fasta format.  Note that, since we use the
                RNA-seq assembly mode, for some genome regions we may get several contigs
                representing alternative transcripts.  Fasta record name of each contig indicates
                its length, coverage, and the group of alternative transcripts to which it belongs.
                See details at 
                http://cab.spbu.ru/files/release3.11.1/rnaspades_manual.html#sec2.4 .
        '''

        assert mode in ('rna', 'meta'), 'Invalid SPAdes mode: {}'.format(mode)

        threads = util.misc.sanitize_thread_count(threads)

        util.file.make_empty(contigs_out)
        contigs_cumul_count = 0

        def is_nonempty_file(f):
            return f and os.path.isfile(f) and os.path.getsize(f) > 0

        libraries = [lib for lib in libraries if is_nonempty_file(lib.reads_fwd)]
        if not libraries:
            log.warning('No non-empty libraries found for SPAdes assembly!')
            return
        log.info('SPAdes assembling from these libs: %s', len(libraries))
        
        kmer_sizes_succeeded = 0
        for kmer_size in util.misc.make_seq(kmer_sizes):
            this_kmer_size_succeeded = False
            # metaSPAdes currently permits only a single paired-end library
            for lib in (libraries if mode=='meta' else (None,)):
                with util.file.tmp_dir('_spades') as spades_dir:
                    log.debug('spades_dir=' + spades_dir)
                    args = ['--rna' if mode=='rna' else '--meta']
                    for lib_num, lib in enumerate(libraries if mode=='rna' else [lib]):
                        assert lib_num < 9
                        lib_pfx = '--pe'+str(lib_num+1)
                        args += [lib_pfx+'-1', lib.reads_fwd, lib_pfx+'-2', lib.reads_bwd ]
                        if is_nonempty_file(lib.reads_unpaired):
                            args += [lib_pfx+'-s', lib.reads_unpaired]

                    if contigs_trusted: args += [ '--trusted-contigs', contigs_trusted ]
                    if contigs_untrusted: args += [ '--untrusted-contigs', contigs_untrusted ]
                    if kmer_size: args += [ '-k', kmer_size ]
                    if spades_opts: args += shlex.split(spades_opts)
                    args += ['-m' + str(mem_limit_gb), '-t', str(threads), '-o', spades_dir]

                    transcripts_fname = os.path.join(spades_dir,
                                                     'contigs.fasta' if mode=='meta' else \
                                                     (('hard_filtered_' if filter_contigs else '')
                                                      + 'transcripts.fasta'))

                    try:
                        self.execute(args=args)
                    except Exception as e:
                        if always_succeed:
                            log.warning('SPAdes failed for k={} lib={}: {}'.format(kmer_size, lib, e))
                            util.file.make_empty(transcripts_fname)
                        else:
                            raise

                    # work around the bug that spades may succeed yet not create the transcripts.fasta file
                    if not os.path.isfile(transcripts_fname):
                        msg = 'SPAdes failed to make transcripts.fasta for k={}'.format(kmer_size)
                        if always_succeed:
                            log.warning(msg)
                            util.file.make_empty(transcripts_fname)
                        else:
                            raise RuntimeError(msg)

                    if min_contig_len:
                        transcripts = Bio.SeqIO.parse(transcripts_fname, 'fasta')
                        transcripts_sans_short = [r for r in transcripts if len(r.seq) >= min_contig_len]
                        transcripts_fname = os.path.join(spades_dir,
                                                         'transcripts_over_{}bp.{}.fasta'.format(min_contig_len,
                                                                                                 contigs_cumul_count))
                        Bio.SeqIO.write(transcripts_sans_short, transcripts_fname, 'fasta')

                    contigs_cumul = os.path.join(spades_dir, 'contigs_cumul.{}.fasta'.format(contigs_cumul_count))
                    contigs_cumul_count += 1

                    util.file.concat(inputFilePaths=(contigs_out, transcripts_fname), outputFilePath=contigs_cumul, append=True)
                    shutil.copyfile(contigs_cumul, contigs_out)

                    this_kmer_size_succeeded = this_kmer_size_succeeded or is_nonempty_file(transcripts_fname)
                # end: with util.file.tmp_dir('_spades') as spades_dir
            # end: for each library

            if is_nonempty_file(transcripts_fname):
                kmer_sizes_succeeded += 1
                if kmer_sizes_succeeded >= max_kmer_sizes:
                    break

        # end: for each kmer size
    # end: def assemble(self, reads_fwd, reads_bwd, contigs_out, reads_unpaired=None, contigs_trusted=None, ...)
# end: class SpadesTool(tools.Tool)


