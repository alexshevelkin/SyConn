# -*- coding: utf-8 -*-
# SyConn - Synaptic connectivity inference toolkit
#
# Copyright (c) 2016 - now
# Max-Planck-Institute of Neurobiology, Munich, Germany
# Authors: Philipp Schubert, Joergen Kornfeld

from knossos_utils import knossosdataset
import numpy as np
knossosdataset._set_noprint(True)
from knossos_utils import chunky
from syconn.extraction import cs_processing_steps
from syconn.extraction import cs_extraction_steps as ces
from syconn import global_params
from syconn.reps.segmentation import SegmentationDataset
from syconn.proc.sd_proc import dataset_analysis, extract_synapse_type
from syconn.proc.ssd_proc import map_synssv_objects
from syconn.extraction import cs_processing_steps as cps
from syconn.handler.config import initialize_logging
from syconn.handler.basics import kd_factory


def run_matrix_export():
    log = initialize_logging('synapse_analysis', global_params.config.working_dir + '/logs/',
                             overwrite=True)
    sd_syn_ssv = SegmentationDataset(working_dir=global_params.config.working_dir,
                                     obj_type='syn_ssv')

    # as an alternative to the skeletons, use vertex predictions or
    # sample_locations, ~3.5h @ 300 cpus
    # TODO: requires speed-up; one could collect properties only for synapses >
    #  probability threshold
    #     synssv_ids = synssv_ids[syn_prob > .5]
    #     ssv_partners = ssv_partners[syn_prob > .5]
    # One could also re-use the cached synssv IDs (computed during mapping of
    # synssv to SSVs) -> saves finding SSV ID indices in synapse arrays (->
    # slow for many synapses)
    cps.collect_properties_from_ssv_partners(global_params.config.working_dir,
                                             debug=False)
    #
    # collect new object attributes collected above partner axoness, celltypes,
    # synapse probabilities etc, no need to compute size/rep_coord etc. ->
    # recompute=False
    dataset_analysis(sd_syn_ssv, compute_meshprops=False,
                     recompute=False)
    log.info('Synapse property collection from SSVs finished.')

    # export_matrix
    log.info('Exporting connectivity matrix now.')
    dest_folder = global_params.config.working_dir + '/connectivity_matrix/'
    cps.export_matrix(dest_folder=dest_folder)
    log.info('Connectivity matrix was epxorted to "{}".'.format(dest_folder))


def run_syn_generation(chunk_size=(512, 512, 512), n_folders_fs=10000,
                       max_n_jobs=None, cube_of_interest_bb=None):
    """

    Parameters
    ----------
    chunk_size :
    n_folders_fs :
    max_n_jobs :
    cube_of_interest_bb : Tuple[np.ndarray]
        Defines the bounding box of the cube to process. By default this is
        set to (np.zoers(3); kd.boundary).

    Returns
    -------

    """
    if max_n_jobs is None:
        max_n_jobs = global_params.NCORE_TOTAL * 2

    log = initialize_logging('synapse_generation', global_params.config.working_dir + '/logs/',
                             overwrite=True)

    kd_seg_path = global_params.config.kd_seg_path
    kd = kd_factory(kd_seg_path)

    if cube_of_interest_bb is None:
        cube_of_interest_bb = [np.zeros(3, dtype=np.int), kd.boundary]
    size = cube_of_interest_bb[1] - cube_of_interest_bb[0] + 1
    offset = cube_of_interest_bb[0]

    # Initital contact site extraction
    cd_dir = global_params.config.working_dir + "/chunkdatasets/cs/"
    # Class that contains a dict of chunks (with coordinates) after initializing it
    cd = chunky.ChunkDataset()
    cd.initialize(kd, kd.boundary, chunk_size, cd_dir,
                  box_coords=[0, 0, 0], fit_box_size=True)

    # POPULATES CS CD with SV contacts
    ces.find_contact_sites(cd, kd_seg_path, size=size, offset=offset)
    ces.extract_agg_contact_sites(cd, global_params.config.working_dir,
                                  n_folders_fs=n_folders_fs, suffix="",
                                  n_chunk_jobs=max_n_jobs, size=size,
                                  offset=offset)
    log.info('Contact site extraction finished.')

    # create overlap dataset between SJ and CS: SegmentationDataset of type 'syn'
    # TODO: write new method which iterates over sj prob. map (KD), CS
    #  ChunkDataset / KD and (optionally) synapse type in parallel and to
    #  create a syn segmentation within from_probmaps_to_objects
    # TODO: SD for cs and sj will not be needed anymore
    cs_sd = SegmentationDataset('cs', working_dir=global_params.config.working_dir,
                                version=0)  # version hard coded
    sj_sd = SegmentationDataset('sj', working_dir=global_params.config.working_dir)
    cs_cset = chunky.load_dataset(cd_dir, update_paths=True)
    #
    # TODO: change stride to n_jobs
    # This creates an SD of type 'syn', currently ~6h, will hopefully be sped up after refactoring
    cs_processing_steps.syn_gen_via_cset(cs_sd, sj_sd, cs_cset, resume_job=False,
                                         nb_cpus=2, n_folders_fs=n_folders_fs,
                                         n_chunk_jobs=max_n_jobs)
    sd = SegmentationDataset("syn", working_dir=global_params.config.working_dir,
                             version="0")
    dataset_analysis(sd, compute_meshprops=False)
    log.info('SegmentationDataset of type "syn" was generated.')

    # This creates an SD of type 'syn_ssv', ~15 min, # TODO: change stride into n_jobs or similar
    cps.combine_and_split_syn(global_params.config.working_dir, resume_job=False,
                              stride=250, cs_gap_nm=global_params.cs_gap_nm,
                              n_folders_fs=n_folders_fs)
    sd_syn_ssv = SegmentationDataset(working_dir=global_params.config.working_dir,
                                     obj_type='syn_ssv')
    dataset_analysis(sd_syn_ssv, compute_meshprops=True)
    log.info('SegmentationDataset of type "syn_ssv" was generated.')

    # This will be replaced by the new method for the 'syn_ssv' generation,
    # ~80 min @ 340 cpus
    extract_synapse_type(sd_syn_ssv, kd_sym_path=global_params.config.kd_sym_path,
                         stride=100,
                         kd_asym_path=global_params.config.kd_asym_path)
    log.info('Synapse type was mapped to "syn_ssv".')

    # ~1h
    cps.map_objects_to_synssv(global_params.config.working_dir)
    log.info('Cellular organelles were mapped to "syn_ssv".')

    cps.classify_synssv_objects(global_params.config.working_dir)
    log.info('Synapse property prediction finished.')

    log.info('Collecting and writing syn-ssv objects to SSV attribute '
             'dictionary.')
    # This needs to be run after `classify_synssv_objects` and before
    # `map_synssv_objects` if the latter uses thresholding for synaptic objects
    dataset_analysis(sd_syn_ssv, compute_meshprops=False, recompute=False)  # just collect new data
    # TODO: decide whether this should happen after prob thresholding or not
    map_synssv_objects()
    log.info('Finished.')
