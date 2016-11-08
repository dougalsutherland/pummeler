from __future__ import division, print_function
import argparse
from glob import glob
import os

import h5py
import numpy as np
import pandas as pd

from .featurize import get_embeddings
from .misc import get_state_embeddings
from .reader import VERSIONS
from .stats import load_stats, save_stats
from .sort import sort_by_region


def main():
    parser = argparse.ArgumentParser(
        description="Reads American Community Survey Public Use Microdata "
                    "Sample files and featurizes them, as in Flaxman, Wang, "
                    "and Smola (KDD 2015). Needs a preprocessing pass over "
                    "the data to sort it into files by region and to collect "
                    "statistics in order to do one-hot encoding and "
                    "standardization. Currently supports only the 2006-10 "
                    "file used in the original paper.",
    )
    subparsers = parser.add_subparsers()

    ############################################################################
    sort = subparsers.add_parser(
        'sort', help="Sort the data by region and collect statistics about it.")
    sort.set_defaults(func=do_sort)

    io = sort.add_argument_group('Input/output options')
    g = io.add_mutually_exclusive_group(required=True)
    g.add_argument('--zipfile', '-z', help="The original ACS PUMS zip file.")
    g.add_argument('--csv-files', '-c', nargs='+',
                   help="CSV files in ACS PUMS format.")

    io.add_argument('out_dir', help='Directory for the sorted features.')

    io.add_argument('--chunksize', type=int, default=10**5, metavar='LINES',
                      help="How much of a CSV file to read at a time; default "
                           "%(default)s.")
    io.add_argument('--stats-only', action='store_true', default=False,
                    help="Only compute the stats, don't do the sorting.")

    fmt = sort.add_argument_group('Format options')
    g = fmt.add_mutually_exclusive_group()
    g.add_argument('--voters-only', action='store_true', default=True,
                   help="Only include citizens who are at least 18 years old "
                        "(default).")
    g.add_argument('--all-people', action='store_false', dest='voters_only',
                   help="Include all records from the files.")
    fmt.add_argument('--version', choices=VERSIONS, default='2006-10',
                      help="The format of the ACS PUMS files in use; default "
                           "%(default)s.")

    ############################################################################
    featurize = subparsers.add_parser(
        'featurize', help="Emit features for a given region.")
    featurize.set_defaults(func=do_featurize)

    io = featurize.add_argument_group('Input/output options')
    io.add_argument('dir', help="The directory where `pummel sort` put stuff.")
    io.add_argument('outfile', nargs='?',
                    help='Where to put embeddings; default DIR/embeddings.npz.')
    io.add_argument('--chunksize', type=int, default=2**13, metavar='LINES',
                      help="How much of a region to process at a time; default "
                           "%(default)s.")

    emb = featurize.add_argument_group('Embedding options')
    emb.add_argument('--skip-rbf', action='store_true', default=False,
                     help="Skip getting the RBF kernel embedding and only get "
                          "the linear one (much, much faster).")
    emb.add_argument('--n-freqs', type=int, default=2048,
                     help='Number of random frequencies to use (half the '
                          'embedding dimension; default %(default)s).')
    emb.add_argument('--bandwidth', type=float,
                     help='Gaussian kernel bandwidth. Default: choose the '
                          'median distance among the random sample saved in '
                          'the stats file.')
    g = emb.add_mutually_exclusive_group()
    g.add_argument('--rff-orthogonal', action='store_true', default=True,
                   help="Use orthogonality in the random features (which "
                        "helps the accuracy of the embedding; default).")
    g.add_argument('--rff-normal', action='store_false', dest='rff_orthogonal',
                   help="Use standard random Fourier features (no "
                        "orthogonality).")
    emb.add_argument('--seed', type=int, default=None,
                     help='Random seed for generating random frequencies. '
                          'Default: none')
    emb.add_argument('--skip-feats', nargs='+', metavar='FEAT_NAME',
                     help="Don't include some features in the embedding.")
    g = emb.add_mutually_exclusive_group()
    g.add_argument('--skip-alloc-flags', action='store_true', default=True,
                   help="Don't include allocation flags (default).")
    g.add_argument('--include-alloc-flags', action='store_false',
                   dest='skip_alloc_flags')
    g = emb.add_mutually_exclusive_group()
    g.add_argument('--do-my-proc', action='store_true', default=False,
                   help="HACK: Do my changes (drop things, cat codes, etc)")
    g.add_argument('--no-my-proc', action='store_false', dest='do_my_proc')
    g = emb.add_mutually_exclusive_group()
    g.add_argument('--do-my-additive', action='store_true', default=False,
                   help="HACK: do additive + some interactions embedding")
    g.add_argument('--no-my-additive', action='store_false', dest='do_my_additive')
    emb.add_argument('--subsets', metavar='PANDAS_QUERY',
                     help="Comma-separated subsets of the data to calculate "
                          "embeddings for, e.g. "
                          "'SEX == 2 & AGEP > 45, SEX == 2 & PINCP < 20000'.")

    ############################################################################
    export = subparsers.add_parser(
        'export', help="Export features in embeddings.npz as CSV files.")
    export.set_defaults(func=do_export)

    io = export.add_argument_group('Input/output options')
    io.add_argument('dir', help="Where to put the outputs.")
    io.add_argument('infile', nargs='?',
                    help="Location of embeddings created by `pummel feauturize`"
                         "; default DIR/embeddings.npz.")
    io.add_argument('--out-name', metavar='BASE',
                    help="Prefix for embedding output files, so that they "
                         "go e.g. in DIR/BASE_linear.csv. Default to "
                         "the basename of INFILE if it's in DIR or "
                         "otherwise 'embeddings'.")

    ############################################################################
    states = subparsers.add_parser(
        'state-features', help="Get state embeddings from existing embeddings.")
    states.set_defaults(func=do_states)

    io = states.add_argument_group('Input/output options')
    io.add_argument('infile', help="The existing region embeddings.")
    io.add_argument('outfile', default=None, nargs='?',
                    help="Where to output; default adds _states to the "
                         "input file name.")

    ############################################################################
    weight_counts = subparsers.add_parser(
        'weight-counts', help="Export total weight per region (approximately "
                              "the number of eligible voters) as a CSV.")
    weight_counts.set_defaults(func=do_weight_counts)

    io = weight_counts.add_argument_group('Input/output options')
    io.add_argument('dir', help="Where the feature files live.")
    io.add_argument('outfile', default=None, nargs='?',
                    help="Where to output; default DIR/weight_counts.csv.")

    ############################################################################
    args = parser.parse_args()
    args.func(args, parser)


def do_sort(args, parser):
    if not os.path.isdir(args.out_dir):
        os.makedirs(args.out_dir)
    stats = sort_by_region(
        args.zipfile or args.csv_files,
        os.path.join(args.out_dir, 'feats_{}.h5'),
        voters_only=args.voters_only, stats_only=args.stats_only,
        adj_inc=True, version=args.version, chunksize=args.chunksize)
    save_stats(os.path.join(args.out_dir, 'stats.h5'), stats)


def do_featurize(args, parser):
    if args.outfile is None:
        args.outfile = os.path.join(args.dir, 'embeddings.npz')
    stats = load_stats(os.path.join(args.dir, 'stats.h5'))
    files = glob(os.path.join(args.dir, 'feats_*.h5'))
    region_names = [os.path.basename(f)[6:-3] for f in files]

    kwargs = dict(
        files=files, stats=stats, chunksize=args.chunksize,
        skip_rbf=args.skip_rbf,
        skip_feats=args.skip_feats, subsets=args.subsets,
        skip_alloc_flags=args.skip_alloc_flags,
        seed=args.seed,
        n_freqs=args.n_freqs, bandwidth=args.bandwidth,
        rff_orthogonal=args.rff_orthogonal,
        do_my_proc=args.do_my_proc, do_my_additive=args.do_my_additive)
    res = dict(region_names=region_names, subset_queries=args.subsets)

    if args.skip_rbf:
        emb_lin, region_weights, feature_names = get_embeddings(**kwargs)
        np.savez(args.outfile,
                 emb_lin=emb_lin, region_weights=region_weights,
                 feature_names=feature_names, **res)
    else:
        emb_lin, emb_rff, rws, fs, bw, fns = get_embeddings(**kwargs)
        np.savez(args.outfile,
                 emb_lin=emb_lin, emb_rff=emb_rff, region_weights=rws,
                 freqs=fs, bandwidth=bw, feature_names=fns, **res)


def do_export(args, parser):
    if args.infile is None:
        args.infile = os.path.join(args.dir, 'embeddings.npz')

    if args.out_name is None:
        rel = os.path.relpath(args.infile, args.dir)
        if '/' in rel:
            args.out_name = 'embeddings'
        else:
            args.out_name = rel[:-4] if rel.endswith('.npz') else rel
    out_pattern = os.path.join(args.dir, args.out_name + '_{}.csv')

    with np.load(args.infile) as data:
        path = out_pattern.format('linear')
        df = pd.DataFrame(data['emb_lin'])
        df.set_index(data['region_names'], inplace=True)
        df.columns = data['feature_names']
        df.to_csv(path, index_label="region")
        print("Linear embeddings saved in {}".format(path))

        if 'emb_rff' in data:
            path = out_pattern.format('rff')
            df = pd.DataFrame(data['emb_rff'])
            df.set_index(data['region_names'], inplace=True)
            df.to_csv(path, index_label="region")
            print("Fourier embeddings saved in {}".format(path))


def do_states(args, parser):
    if args.outfile is None:
        inf = args.infile
        if args.infile.endswith('.npz'):
            inf = args.infile[:-4]
        args.outfile = inf + '_states.npz'

    np.savez(args.outfile, **get_state_embeddings(args.infile))


def do_weight_counts(args, parser):
    if args.outfile is None:
        args.outfile = os.path.join(args.dir, 'weight_counts.csv')

    mapping = {}
    for fn in os.listdir(args.dir):
        if fn.startswith('feats_') and fn.endswith('.h5'):
            region = fn[len('feats_'):-len('.h5')]
            with h5py.File(os.path.join(args.dir, fn), 'r') as f:
                mapping[region] = f['total_wt'][()]

    df = pd.DataFrame.from_dict(mapping, orient='index')
    df.columns = ['total_wt']
    df.index.names = ['region']
    df.to_csv(args.outfile)
