# -*- coding: utf-8 -*-
# SyConn - Synaptic connectivity inference toolkit
#
# Copyright (c) 2016 - now
# Max-Planck-Institute of Neurobiology, Munich, Germany
# Authors: Philipp Schubert

import os
import shutil
from typing import Optional

import networkx as nx
import numpy as np

from syconn import global_params
from syconn.handler.basics import chunkify
from syconn.handler.config import initialize_logging
from syconn.handler.prediction_pts import predict_glia_ssv, predict_celltype_ssd, infere_cell_morphology_ssd
from syconn.mp import batchjob_utils as qu
from syconn.proc.glia_splitting import qsub_glia_splitting, collect_glia_sv, write_glia_rag, transform_rag_edgelist2pkl
from syconn.proc.graphs import create_ccsize_dict
from syconn.proc.graphs import split_subcc_join
from syconn.reps.segmentation import SegmentationDataset
from syconn.reps.segmentation_helper import find_missing_sv_views
from syconn.reps.super_segmentation import SuperSegmentationDataset


def run_morphology_embedding(max_n_jobs: Optional[int] = None):
    """
    Infer local morphology embeddings for all neuron reconstructions base on
    triplet-loss trained cellular morphology learning network (tCMN).
    The point based model is trained with the pts_loader_scalar (used for celltypes)

    Args:
        max_n_jobs: Number of parallel jobs.

    Notes:
        Requires :func:`~syconn.exec.exec_init.run_create_neuron_ssd`, :func:`~run_neuron_rendering` and
        :func:`~syconn.exec.skeleton.run_skeleton_generation`.
    """
    if max_n_jobs is None:
        max_n_jobs = global_params.config.ngpu_total * 2
    log = initialize_logging('morphology_embedding', global_params.config.working_dir
                             + '/logs/', overwrite=False)
    ssd = SuperSegmentationDataset(working_dir=global_params.config.working_dir)
    pred_key_appendix = ""

    multi_params = np.array(ssd.ssv_ids, dtype=np.uint)
    nb_svs_per_ssv = np.array([len(ssd.mapping_dict[ssv_id]) for ssv_id in ssd.ssv_ids])
    # sort ssv ids according to their number of SVs (descending)
    multi_params = multi_params[np.argsort(nb_svs_per_ssv)[::-1]]

    if not qu.batchjob_enabled() and global_params.config.use_point_models:
        ssd_kwargs = dict(working_dir=ssd.working_dir, config=ssd.config)
        ssv_params = [dict(ssv_id=ssv_id, **ssd_kwargs) for ssv_id in multi_params]
        infere_cell_morphology_ssd(ssv_params)
    else:
        multi_params = chunkify(multi_params, max_n_jobs)
        # add ssd parameters
        multi_params = [(ssv_ids, pred_key_appendix) for ssv_ids in multi_params]
        qu.batchjob_script(multi_params, "generate_morphology_embedding",
                           n_cores=global_params.config['ncores_per_node'] // global_params.config['ngpus_per_node'],
                           log=log, suffix="", additional_flags="--gres=gpu:1", remove_jobfolder=True)
    log.info('Finished extraction of cell morphology embeddings.')


def run_celltype_prediction(max_n_jobs_gpu: Optional[int] = None):
    """
    Run the celltype inference based on the ``img2scalar`` CMN.

    Args:
        max_n_jobs_gpu: Number of parallel GPU jobs.

    Notes:
        Requires :func:`~syconn.exec.exec_init.run_create_neuron_ssd` and :func:`~run_neuron_rendering`.
    """
    if max_n_jobs_gpu is None:
        max_n_jobs_gpu = global_params.config.ngpu_total * 3 if qu.batchjob_enabled() else 1
    log = initialize_logging('celltype_prediction', global_params.config.working_dir + '/logs/',
                             overwrite=False)
    ssd = SuperSegmentationDataset(working_dir=global_params.config.working_dir)
    multi_params = ssd.ssv_ids
    if not qu.batchjob_enabled() and global_params.config.use_point_models:
        predict_celltype_ssd(ssd_kwargs=dict(working_dir=global_params.config.working_dir), ssv_ids=multi_params)
    else:
        np.random.seed(0)
        np.random.shuffle(multi_params)
        multi_params = chunkify(multi_params, max_n_jobs_gpu)
        # job parameter will be read sequentially, i.e. in order to provide only
        # one list as parameter one needs an additonal axis
        multi_params = [(ixs,) for ixs in multi_params]
        qu.batchjob_script(multi_params, "predict_cell_type", log=log, suffix="", additional_flags="--gres=gpu:1",
                           n_cores=global_params.config['ncores_per_node'] // global_params.config['ngpus_per_node'],
                           remove_jobfolder=True)
    log.info(f'Finished prediction of {len(ssd.ssv_ids)} SSVs.')


def run_semsegaxoness_prediction(max_n_jobs_gpu: Optional[int] = None):
    """
    Infer and map semantic segmentation of the 2D projections onto the cell reconstruction mesh
    (``ssv.label_dict('vertex')``) via ``semseg_of_sso_nocache``.
    The following skeleton attributes are generated by ``semsegaxoness2skel`` and available in
    :py:attr:`~syconn.reps.super_segmentation_object.SuperSegmentationObject.skeleton`:
        * "axoness": Vertex predictions mapped to skeleton (see
          ``global_params.config['compartments']['map_properties_semsegax']``.
        * "axoness_avg10000": Sliding window average along skeleton (10um traversal length).
        * "axoness_avg10000_comp_maj": Majority vote on connected components after removing the
          soma.

    Args:
        max_n_jobs_gpu: Number of parallel GPU jobs.

    Returns:

    """
    if max_n_jobs_gpu is None:
        max_n_jobs_gpu = global_params.config.ngpu_total * 10 if qu.batchjob_enabled() else 1
    if qu.batchjob_enabled():
        n_cores = global_params.config['ncores_per_node'] // global_params.config['ngpus_per_node']
    else:
        n_cores = global_params.config['ncores_per_node']
    log = initialize_logging('compartment_prediction', global_params.config.working_dir + '/logs/',
                             overwrite=False)
    ssd = SuperSegmentationDataset(working_dir=global_params.config.working_dir)
    np.random.seed(0)
    multi_params = ssd.ssv_ids
    np.random.shuffle(multi_params)
    multi_params = chunkify(multi_params, max_n_jobs_gpu)
    # job parameter will be read sequentially, i.e. in order to provide only
    # one list as parameter one needs an additonal axis
    multi_params = [(ixs,) for ixs in multi_params]

    path_to_out = qu.batchjob_script(multi_params, 'predict_axoness_semseg', log=log,
                                     suffix="", additional_flags="--gres=gpu:1",
                                     n_cores=n_cores, remove_jobfolder=False)
    log.info(f'Finished prediction of {len(ssd.ssv_ids)} SSVs.')
    shutil.rmtree(os.path.abspath(path_to_out + "/../"), ignore_errors=True)


def run_semsegspiness_prediction(max_n_jobs_gpu: Optional[int] = None):
    """
    Will store semantic spine labels inside``ssv.label_dict('vertex')['spiness]``.

    Args:
        max_n_jobs_gpu: Number of parallel GPU jobs. Used for the inference.
    """
    if max_n_jobs_gpu is None:
        max_n_jobs_gpu = global_params.config.ngpu_total * 10 if qu.batchjob_enabled() else 1
    log = initialize_logging('compartment_prediction', global_params.config.working_dir
                             + '/logs/', overwrite=False)
    ssd = SuperSegmentationDataset(working_dir=global_params.config.working_dir)
    np.random.seed(0)
    multi_params = ssd.ssv_ids
    np.random.shuffle(multi_params)
    multi_params = chunkify(multi_params, max_n_jobs_gpu)
    # job parameter will be read sequentially, i.e. in order to provide only
    # one list as parameter one needs an additional axis
    multi_params = [(ixs,) for ixs in multi_params]

    predict_func = 'predict_spiness_semseg'
    qu.batchjob_script(multi_params, predict_func, log=log,
                       n_cores=global_params.config['ncores_per_node'] // global_params.config['ngpus_per_node'],
                       suffix="", additional_flags="--gres=gpu:1", remove_jobfolder=True)
    log.info('Finished spine prediction.')


def run_glia_prediction_pts(max_n_jobs_gpu: Optional[int] = None):
    """
    Predict glia and neuron supervoxels with point cloud based convolutional networks.

    Args:
        max_n_jobs_gpu:

    Notes:
        Requires :func:`~syconn.exec_init.init_cell_subcell_sds`.
    """
    if max_n_jobs_gpu is None:
        max_n_jobs_gpu = global_params.config.ngpu_total * 10
    log = initialize_logging('glia_separation', global_params.config.working_dir + '/logs/', overwrite=False)
    pred_key = "glia_probas"

    log.info("Preparing RAG.")
    G = nx.read_edgelist(global_params.config.pruned_rag_path, nodetype=np.uint)
    cc_gs = sorted(list(nx.connected_component_subgraphs(G)), key=len, reverse=True)

    # generate parameter for view rendering of individual SSV
    sds = SegmentationDataset("sv", working_dir=global_params.config.working_dir)
    sv_size_dict = {}
    bbs = sds.load_cached_data('bounding_box') * sds.scaling
    for ii in range(len(sds.ids)):
        sv_size_dict[sds.ids[ii]] = bbs[ii]
    ccsize_dict = create_ccsize_dict(cc_gs, sv_size_dict, is_connected_components=True)

    log.info("Preparing cells for glia prediction.")
    lo_first_n = global_params.config['glia']['subcc_chunk_size_big_ssv']
    max_nb_sv = global_params.config['glia']['subcc_size_big_ssv'] + 2 * (lo_first_n - 1)
    multi_params = []
    # Store supervoxels belonging to one cell and whether they have been partitioned or not
    for g in cc_gs:
        if g.number_of_nodes() > global_params.config['glia']['rendering_max_nb_sv']:
            # partition large SSVs into small chunks with overlap
            parts = split_subcc_join(g, max_nb_sv, lo_first_n=lo_first_n)
            multi_params.extend([(p, g.subgraph(p), True) for p in parts])
        elif ccsize_dict[list(g.nodes())[0]] < global_params.config['glia']['min_cc_size_ssv']:
            pass  # ignore this CC
        else:
            multi_params.append((list(g.nodes()), g, False))

    # only append to this key if needed (e.g. different versions)
    np.random.seed(0)
    np.random.shuffle(multi_params)
    # job parameter will be read sequentially, i.e. in order to provide only
    # one list as parameter one needs an additional axis
    if not qu.batchjob_enabled():
        # Default SLURM fallback with Popen keeps freezing.
        working_dir = global_params.config.working_dir
        ssv_params = []
        partitioned = dict()
        for sv_ids, g, was_partitioned in multi_params:
            ssv_params.append(dict(ssv_id=sv_ids[0], sv_ids=sv_ids, working_dir=working_dir, sv_graph=g, version='tmp'))
            partitioned[sv_ids[0]] = was_partitioned
        postproc_kwargs = dict(pred_key=pred_key, lo_first_n=lo_first_n, partitioned=partitioned)
        predict_glia_ssv(ssv_params, postproc_kwargs=postproc_kwargs)
    else:
        multi_params = [(el, pred_key) for el in chunkify(multi_params, max_n_jobs_gpu)]
        qu.batchjob_script(multi_params, 'predict_glia_pts', log=log,
                           n_cores=global_params.config['ncores_per_node'] // global_params.config['ngpus_per_node'],
                           suffix="", additional_flags="--gres=gpu:1", remove_jobfolder=True)
    log.info('Finished glia prediction.')


def run_glia_prediction():
    """
    Predict glia supervoxels based on the ``img2scalar`` CMN.

    Notes:
        Requires :func:`~syconn.exec_init.init_cell_subcell_sds` and
        :func:`~run_glia_rendering`.
    """
    log = initialize_logging('glia_separation', global_params.config.working_dir + '/logs/',
                             overwrite=False)
    # only append to this key if needed (e.g. different versions)
    pred_key = "glia_probas"

    # Load initial RAG from  Knossos mergelist text file.
    g = nx.read_edgelist(global_params.config.pruned_rag_path, nodetype=np.uint)
    all_sv_ids_in_rag = np.array(list(g.nodes()), dtype=np.uint)

    log.debug('Found {} CCs with a total of {} SVs in inital RAG.'.format(
        nx.number_connected_components(g), g.number_of_nodes()))
    # chunk them
    sd = SegmentationDataset("sv", working_dir=global_params.config.working_dir)
    multi_params = chunkify(sd.so_dir_paths, global_params.config.ngpu_total * 2)
    # get model properties
    model_kwargs = 'get_glia_model_e3'
    # all other kwargs like obj_type='sv' and version are the current SV
    # SegmentationDataset by default
    so_kwargs = dict(working_dir=global_params.config.working_dir)
    # for glia views set woglia to False (because glia are included),
    #  raw_only to True
    pred_kwargs = dict(woglia=False, pred_key=pred_key, verbose=False, raw_only=True)

    multi_params = [[par, model_kwargs, so_kwargs, pred_kwargs] for par in
                    multi_params]
    n_cores = global_params.config['ncores_per_node'] // global_params.config['ngpus_per_node']
    qu.batchjob_script(multi_params, "predict_sv_views_chunked_e3", log=log,
                       script_folder=None, n_cores=n_cores,
                       suffix="_glia", additional_flags="--gres=gpu:1",
                       remove_jobfolder=True)
    log.info('Finished glia prediction. Checking completeness.')
    res = find_missing_sv_views(sd, woglia=False, n_cores=global_params.config['ncores_per_node'])
    missing_contained_in_rag = np.intersect1d(res, all_sv_ids_in_rag)
    if len(missing_contained_in_rag) != 0:
        msg = "Not all SVs were predicted! {}/{} missing:\n" \
              "{}".format(len(missing_contained_in_rag), len(all_sv_ids_in_rag),
                          missing_contained_in_rag[:100])
        log.error(msg)
        raise ValueError(msg)
    else:
        log.info('Success.')


def run_glia_splitting():
    """
    Uses the pruned RAG at ``global_params.config.pruned_rag_path`` (stored as edge list .bz2 file)
    which is  computed in :func:`~syconn.exec.exec_init.init_cell_subcell_sds` to split glia
    fragments from neuron reconstructions and separate those and entire glial cells from
    the neuron supervoxel graph.

    Stores neuron RAG at ``"{}/glia/neuron_rag{}.bz2".format(global_params.config.working_dir,
    suffix)`` which is then used by :func:`~syconn.exec.exec_init.run_create_neuron_ssd`.

    Todo:
        * refactor how splits are stored, currently those are stored at ssv_tmp

    Notes:
        Requires :func:`~syconn.exec_init.init_cell_subcell_sds`,
        :func:`~run_glia_rendering` and :func:`~run_glia_prediction`.
    """
    log = initialize_logging('glia_separation', global_params.config.working_dir + '/logs/',
                             overwrite=False)
    G = nx.read_edgelist(global_params.config.pruned_rag_path, nodetype=np.uint)
    log.debug('Found {} CCs with a total of {} SVs in inital RAG.'.format(
        nx.number_connected_components(G), G.number_of_nodes()))

    if not os.path.isdir(global_params.config.working_dir + "/glia/"):
        os.makedirs(global_params.config.working_dir + "/glia/")
    transform_rag_edgelist2pkl(G)

    # first perform glia splitting based on multi-view predictions, results are
    # stored at SuperSegmentationDataset ssv_gliaremoval
    qsub_glia_splitting()

    # collect all neuron and glia SVs and store them in numpy array
    collect_glia_sv()

    # # here use reconnected RAG or initial rag
    recon_nx = G
    # create glia / neuron RAGs
    write_glia_rag(recon_nx, global_params.config['glia']['min_cc_size_ssv'], log=log)
    log.info("Finished glia splitting. Resulting neuron and glia RAGs are stored at {}."
             "".format(global_params.config.working_dir + "/glia/"))
