# -*- coding: utf-8 -*-
# SyConn - Synaptic connectivity inference toolkit
#
# Copyright (c) 2016 - now
# Max Planck Institute of Neurobiology, Martinsried, Germany
# Authors: Sven Dorkenwald, Philipp Schubert, Joergen Kornfeld
import copy
import os
import time

from . import log_reps
from . import segmentation
from .rep_helper import assign_rep_values, colorcode_vertices, surface_samples
from .segmentation import SegmentationObject
from .segmentation_helper import load_skeleton, find_missing_sv_views, \
    find_missing_sv_attributes, find_missing_sv_skeletons, load_so_attr_bulk
from .. import global_params
from ..handler.basics import kd_factory
from ..handler.multiviews import generate_rendering_locs
from ..mp.mp_utils import start_multiprocess_obj, start_multiprocess_imap
from ..proc.graphs import create_graph_from_coords, stitch_skel_nx
from ..proc.meshes import write_mesh2kzip
from ..proc.rendering import render_sso_coords

try:
    from ..proc.in_bounding_boxC import in_bounding_box
except ImportError:
    from ..proc.in_bounding_box import in_bounding_box
from typing import Dict, List, Union, Optional, Tuple, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import super_segmentation

from collections import Counter
from multiprocessing.pool import ThreadPool
import networkx as nx
from numba import jit
import numpy as np
import scipy
import scipy.ndimage
from scipy import spatial
from knossos_utils.skeleton_utils import annotation_to_nx_graph, \
    load_skeleton as load_skeleton_kzip, Skeleton, SkeletonAnnotation, SkeletonNode
from collections.abc import Iterable

try:
    from knossos_utils import mergelist_tools
except ImportError:
    from knossos_utils import mergelist_tools_fallback as mergelist_tool

from skimage.segmentation import watershed
from skimage.feature import peak_local_max
from scipy import ndimage


def majority_vote(anno, prop, max_dist):
    """
    Smoothes (average using sliding window of 2 times max_dist and majority
    vote) property prediction in annotation, whereas for axoness somata are
    untouched.
    Args:
        anno:  SkeletonAnnotation
        prop:str
            which property to average
        max_dist: int
            maximum distance (in nm) for sliding window used in majority voting

    Returns:

    """
    old_anno = copy.deepcopy(anno)
    nearest_nodes_list = nodes_in_pathlength(old_anno, max_dist)
    for nodes in nearest_nodes_list:
        curr_node_id = nodes[0].getID()
        new_node = anno.getNodeByID(curr_node_id)
        if prop == "axoness":
            if int(new_node.data["axoness_pred"]) == 2:
                new_node.data["axoness_pred"] = 2
                continue
        property_val = [int(n.data[prop + '_pred']) for n in nodes if
                        int(n.data[prop + '_pred']) != 2]
        counter = Counter(property_val)
        new_ax = counter.most_common()[0][0]
        new_node.setDataElem(prop + '_pred', new_ax)


def nodes_in_pathlength(anno, max_path_len):
    """
    Find nodes reachable in max_path_len from source node, calculated for
    every node in anno.
    Args:
        anno: AnnotationObject
        max_path_len: float
            Maximum distance from source node

    Returns: list of lists containing reachable nodes in max_path_len where
        outer list has length len(anno.getNodes())

    """
    skel_graph = annotation_to_nx_graph(anno)
    list_reachable_nodes = []
    for source_node in anno.getNodes():
        reachable_nodes = [source_node]
        curr_path_length = 0.0
        for edge in nx.bfs_edges(skel_graph, source_node):
            next_node = edge[1]
            next_node_coord = np.array(next_node.getCoordinate_scaled())
            curr_vec = next_node_coord - np.array(edge[0].getCoordinate_scaled())
            curr_path_length += np.linalg.norm(curr_vec)
            if curr_path_length > max_path_len:
                break
            reachable_nodes.append(next_node)
        list_reachable_nodes.append(reachable_nodes)
    return list_reachable_nodes


def predict_sso_celltype(sso: 'super_segmentation.SuperSegmentationObject',
                         model: Any, nb_views_model: int = 20, use_syntype=True,
                         overwrite: bool = False, pred_key_appendix="",
                         da_equals_tan=True):
    """
    Celltype prediction based on local views and synapse type ratio feature.
    Uses on file system cached views (also used for axon and spine prediction).
    See `celltype_of_sso_nocache` for 'on-the-fly' prediction, which renders
    views from scratch given their window size etc.
    :func:`~sso_views_to_modelinput` is used to create the random view subsets.
    The final prediction is the majority class of all subset predictions.

    Args:
        sso: SuperSegmentationObject
        model: nn.Module
        nb_views_model: int
        use_syntype: bool
        overwrite:  bool
             Use the type of the pre-synapses.
        pred_key_appendix:  str
        da_equals_tan:

    Returns:

    """
    sso.load_attr_dict()
    pred_key = "celltype_cnn_e3" + pred_key_appendix
    if not overwrite and pred_key in sso.attr_dict:
        return
    from ..handler.prediction import naive_view_normalization_new
    inp_d = sso_views_to_modelinput(sso, nb_views_model)
    inp_d = naive_view_normalization_new(inp_d)
    if global_params.config.syntype_available and use_syntype:
        synsign_ratio = np.array([[syn_sign_ratio_celltype(sso, comp_types=[1, ]),
                                   syn_sign_ratio_celltype(sso, comp_types=[0, ])]]
                                 * len(inp_d))
        res = model.predict_proba((inp_d, synsign_ratio))
    else:
        res = model.predict_proba(inp_d)

    # DA and TAN are type modulatory, if this is changes, also change `certainty_celltype`, `celltype_of_sso_nocache`
    if da_equals_tan:
        # accumulate evidence for DA and TAN
        res[:, 1] += res[:, 6]
        # remove TAN in proba array
        res = np.delete(res, [6], axis=1)
        # INT is now at index 6 -> label 6 is INT

    clf = np.argmax(res, axis=1)
    if np.max(clf) >= 7:
        raise ValueError('Unknown cell type predicted.')
    major_dec = np.zeros(7)
    for ii in range(len(major_dec)):
        major_dec[ii] = np.sum(clf == ii)
    major_dec /= np.sum(major_dec)
    pred = np.argmax(major_dec)
    sso.attr_dict[pred_key] = pred
    sso.attr_dict[f"{pred_key}_probas"] = res
    sso.save_attributes([pred_key], [pred])
    sso.save_attributes([f"{pred_key}_probas"], [res])


def sso_views_to_modelinput(sso: 'super_segmentation.SuperSegmentationObject',
                            nb_views: int, view_key: Optional[str] = None) -> np.ndarray:
    """
    Converts the 2D projections views of an
    :class:`~syconn.reps.super_segmentation_object.SuperSegmentationObject` into
    random subsets of views each of size `nb_views`. Used for cell type inference.

    todo:
        * shuffle after reshaping from (#multi-view locations, 4 channels, #nb_views, 128, 256) to
          (#multi-view locations * #nb_views, 4 channels, 128, 256)?
    Args:
        sso: Cell reconstruction object.
        nb_views: Number of views in each view subset.
        view_key: Key of the stored views.

    Returns: An array of random view subsets of all 2D projections contained in the
        cell reconstruction. Shape: (#subsets, 4 channels, nb_views, 128, 256)

    """
    np.random.seed(0)
    assert len(sso.sv_ids) > 0
    views = sso.load_views(view_key=view_key)
    np.random.shuffle(views)
    # view shape: (#multi-view locations, 4 channels, #nb_views, 128, 256)
    views = views.swapaxes(1, 0).reshape((4, -1, 128, 256))
    assert views.shape[1] > 0
    if views.shape[1] < nb_views:
        rand_ixs = np.random.choice(views.shape[1], nb_views - views.shape[1])
        views = np.append(views, views[:, rand_ixs], axis=1)
    nb_samples = np.floor(views.shape[1] / nb_views)
    assert nb_samples > 0
    out_d = views[:, :int(nb_samples * nb_views)]
    out_d = out_d.reshape((4, -1, nb_views, 128, 256)).swapaxes(1, 0)
    return out_d


def radius_correction_found_vertices(sso: 'super_segmentation.SuperSegmentationObject',
                                     plump_factor: int = 1, num_found_vertices: int = 10):
    """
    Algorithm finds two nearest vertices and takes the median of the
    distances for every node.

    Args:
        sso: SuperSegmentationObject
        plump_factor: int
            multiplication factor for the radius
        num_found_vertices: int
            number of closest vertices queried for the node

    Returns: skeleton with diameters estimated

    """
    skel_node = sso.skeleton['nodes']
    diameters = sso.skeleton['diameters']

    vert_sparse = sso.mesh[1].reshape((-1, 3))
    tree = spatial.cKDTree(vert_sparse)
    dists, all_found_vertices_ixs = tree.query(skel_node * sso.scaling,
                                               num_found_vertices)

    for ii, el in enumerate(skel_node):
        diameters[ii] = np.median(dists[ii]) * 2 / 10

    sso.skeleton['diameters'] = diameters * plump_factor
    return sso.skeleton


def get_sso_axoness_from_coord(sso, coord, k=5):
    """
    Finds k nearest neighbor nodes within sso skeleton and returns majority
    class of dendrite (0), axon (1) or soma (2).

    Args:
        sso: SuperSegmentationObject
        coord: np.array
            unscaled coordinate
        k: int
            Number of nearest neighbors on which the majority vote is computed.

    Returns: int

    """
    coord = np.array(coord) * np.array(sso.scaling)
    sso.load_skeleton()
    kdt = spatial.cKDTree(sso.skeleton["nodes"] * np.array(sso.scaling))
    dists, ixs = kdt.query(coord, k=k)
    ixs = ixs[dists != np.inf]
    axs = sso.skeleton["axoness"][ixs]
    cnt = Counter(axs)
    return cnt.most_common(n=1)[0][0]


def load_voxels_downsampled(sso, downsampling=(2, 2, 1), nb_threads=10):
    def _load_sv_voxels_thread(args):
        sv_id = args[0]
        sv = segmentation.SegmentationObject(sv_id,
                                             obj_type="sv",
                                             version=sso.version_dict[
                                                 "sv"],
                                             working_dir=sso.working_dir,
                                             config=sso.config,
                                             voxel_caching=False)
        if sv.voxels_exist:
            box = [np.array(sv.bounding_box[0] - sso.bounding_box[0],
                            dtype=np.int)]

            box[0] /= downsampling
            size = np.array(sv.bounding_box[1] -
                            sv.bounding_box[0], dtype=np.float)
            size = np.ceil(size.astype(np.float) /
                           downsampling).astype(np.int)

            box.append(box[0] + size)

            sv_voxels = sv.voxels
            if not isinstance(sv_voxels, int):
                sv_voxels = sv_voxels[::downsampling[0],
                            ::downsampling[1],
                            ::downsampling[2]]

                voxels[box[0][0]: box[1][0],
                box[0][1]: box[1][1],
                box[0][2]: box[1][2]][sv_voxels] = True

    downsampling = np.array(downsampling, dtype=np.int)

    if len(sso.sv_ids) == 0:
        return None

    voxel_box_size = sso.bounding_box[1] - sso.bounding_box[0]
    voxel_box_size = voxel_box_size.astype(np.float)

    voxel_box_size = np.ceil(voxel_box_size / downsampling).astype(np.int)

    voxels = np.zeros(voxel_box_size, dtype=np.bool)

    multi_params = []
    for sv_id in sso.sv_ids:
        multi_params.append([sv_id])

    if nb_threads > 1:
        pool = ThreadPool(nb_threads)
        pool.map(_load_sv_voxels_thread, multi_params)
        pool.close()
        pool.join()
    else:
        map(_load_sv_voxels_thread, multi_params)

    return voxels


def create_new_skeleton(sv_id, sso):
    """

    Args:
        sv_id:
        sso:

    Returns:

    """
    so = SegmentationObject(sv_id, obj_type="sv", version=sso.version_dict["sv"], working_dir=sso.working_dir,
                            config=sso.config)
    so.enable_locking = False
    so.load_attr_dict()
    skel = load_skeleton(so)
    return skel['nodes'], skel['diameters'], skel['edges']


def convert_coord(coord_list, scal):
    """

    Args:
        coord_list:
        scal:

    Returns:

    """
    return np.array([coord_list[1] + 1, coord_list[0] + 1,
                     coord_list[2] + 1]) * np.array(scal)


def prune_stub_branches(sso=None, nx_g=None, scal=None, len_thres=1000,
                        preserve_annotations=True):
    """
     Removes short stub branches, that are often added by annotators but
    hardly represent true morphology.

    Args:
        sso: SuperSegmentationObject
        nx_g: network kx graph
        scal: array of size 3
            the scaled up factor
        len_thres: int
            threshold of the length below which it will be pruned
        preserve_annotations: bool

    Returns: pruned MST

    """
    if scal is None:
        scal = global_params.config['scaling']
    pruning_complete = False

    if preserve_annotations:
        new_nx_g = nx_g.copy()
    else:
        new_nx_g = nx_g

    # find all tip nodes in an anno, ie degree 1 nodes
    while not pruning_complete:
        nx_g = new_nx_g.copy()
        end_nodes = list({k for k, v in dict(nx_g.degree()).items() if v == 1})
        # DFS to first branch node
        for end_node in end_nodes:
            prune_nodes = []
            for curr_node in nx.traversal.dfs_preorder_nodes(nx_g, end_node):
                if nx_g.degree(curr_node) > 2:
                    loc_end = convert_coord(nx_g.node[end_node]['position'], scal)
                    loc_curr = convert_coord(nx_g.node[curr_node]['position'], scal)
                    b_len = np.linalg.norm(loc_end - loc_curr)

                    if b_len < len_thres:
                        # remove this stub, i.e. prune the nodes that were
                        # collected on our way to the branch point
                        for prune_node in prune_nodes:
                            new_nx_g.remove_node(prune_node)
                        break
                    else:
                        break
                prune_nodes.append(curr_node)
        if len(new_nx_g.nodes) == len(nx_g.nodes):
            pruning_complete = True
    # TODO: uncomment, or fix by using alternative method
    if nx.number_connected_components(new_nx_g) != 1:
        msg = 'Pruning of SV skeletons failed during "prune_stub_branches' \
              '" with {} connected components. Please check the underlying' \
              ' SSV {}. Performing stitching method to add missing edg' \
              'es recursively.'.format(nx.number_connected_components(new_nx_g),
                                       sso.id)
        new_nx_g = stitch_skel_nx(new_nx_g)
        log_reps.critical(msg)
    # # Important assert. Please don't remove
    # assert nx.number_connected_components(new_nx_g) == 1,\
    #     'Additional connected components created after pruning!'

    for e in new_nx_g.edges:
        w = np.linalg.norm((new_nx_g.node[e[0]]['position'] -
                            new_nx_g.node[e[1]]['position']) * scal)
        new_nx_g[e[0]][e[1]]['weight'] = w
    new_nx_g = nx.minimum_spanning_tree(new_nx_g)
    if sso is not None:
        sso = from_netkx_to_sso(sso, new_nx_g)
    return sso, new_nx_g


def from_netkx_to_sso(sso, skel_nx):
    """

    Args:
        sso:
        skel_nx:

    Returns:

    """
    sso.skeleton = dict()
    sso.skeleton['nodes'] = np.array([skel_nx.node[ix]['position'] for ix in
                                      skel_nx.nodes()], dtype=np.uint32)
    sso.skeleton['diameters'] = np.zeros(len(sso.skeleton['nodes']),
                                         dtype=np.float)

    assert nx.number_connected_components(skel_nx) == 1

    # Important bit, please don't remove (needed after pruning)
    temp_edges = np.array(list(skel_nx.edges())).reshape(-1)
    temp_edges_sorted = np.unique(np.sort(temp_edges))
    temp_edges_dict = {}

    for ii, ix in enumerate(temp_edges_sorted):
        temp_edges_dict[ix] = ii

    temp_edges = [temp_edges_dict[ix] for ix in temp_edges]

    temp_edges = np.array(temp_edges, dtype=np.uint).reshape([-1, 2])
    sso.skeleton['edges'] = temp_edges

    return sso


def create_sso_skeletons_wrapper(ssvs: List['super_segmentation.SuperSegmentationObject'],
                                 dest_paths: Optional[str] = None, nb_cpus: Optional[int] = None,
                                 map_myelin: bool = False, save: bool = True):
    """
    Used within :func:`~syconn.reps.super_segmentation_object.SuperSegmentationObject`
    to generate a skeleton representation of the cell. If
    ``global_params.config.allow_ssv_skel_gen = True``, the skeleton will be created using
    a sampling procedure based on the cell surface, i.e. the resulting skeleton
    might fall out of the cell's segmentation and its nodes will always be close
    to the cell surface.
    If ``global_params.config.allow_ssv_skel_gen = False``, supervoxel skeleton must already
    exist and those will be pruned, stitched and finally a per-node diameter estimation will
    be performed.
    This method will invoke ``ssv.save_skeleton`` and the results will also be available in every
    object of ``ssvs`` via ``ssv.skeleton``.

    Args:
        ssvs: An iterable of cell reconstruction objects.
        dest_paths: Paths to kzips for each object in `ssvs`.
        nb_cpus: Number of CPUs used for every ``ssv`` in `ssvs`.
        map_myelin: Map myelin predictions at every ``ssv.skeleton["nodes"] stored as
            ``ssv.skeleton["myelin"]`` with :func:`~map_myelin2coords`. The myelin
            predictions are smoothed via a sliding window majority vote
            (see :func:`~majorityvote_skeleton_property`) with a traversal distance
             of 10 micrometers.
        save: Write generated skeleton.

    Todo:
        * Add sliding window majority vote for smoothing myelin prediction to ``global_params``.
    """
    if nb_cpus is None:
        nb_cpus = global_params.config['ncores_per_node']
    if dest_paths is not None:
        if not isinstance(dest_paths, Iterable):
            raise ValueError('Destination paths given but are not iterable.')
    else:
        dest_paths = [None for _ in ssvs]
    use_new_renderings_locs = global_params.config.use_new_renderings_locs
    for ssv, dest_path in zip(ssvs, dest_paths):
        ssv.nb_cpus = nb_cpus
        if not global_params.config.allow_ssv_skel_gen:
            # This merges existing SV skeletons - SV skeletons must exist
            ssv = create_sso_skeleton_fast(ssv)
        else:
            # TODO: add parameter to config
            verts = ssv.mesh[1].reshape(-1, 3)
            # choose random subset of surface vertices
            np.random.seed(0)
            ixs = np.arange(len(verts))
            np.random.shuffle(ixs)
            ixs = ixs[:int(0.5 * len(ixs))]
            if use_new_renderings_locs:
                locs = generate_rendering_locs(verts[ixs], 1000)
            else:
                locs = surface_samples(verts[ixs], bin_sizes=(1000, 1000, 1000), max_nb_samples=10000, r=500)
            g = create_graph_from_coords(locs, mst=True, force_single_cc=True)

            if g.number_of_edges() == 1:
                edge_list = np.array(list(g.edges()))
            else:
                edge_list = np.array(g.edges())
            del g
            if edge_list.ndim != 2:
                raise ValueError("Edge list ist not a 2D array: {}\n{}".format(
                    edge_list.shape, edge_list))
            ssv.skeleton = dict()
            ssv.skeleton["nodes"] = (locs / np.array(ssv.scaling)).astype(np.int)
            ssv.skeleton["edges"] = edge_list
            ssv.skeleton["diameters"] = np.ones(len(locs))
        if map_myelin:
            try:
                ssv.skeleton["myelin"] = map_myelin2coords(ssv.skeleton["nodes"], mag=4)
                majorityvote_skeleton_property(ssv, prop_key='myelin')
            except Exception as e:
                raise Exception(f'Myelin mapping in {ssv} failed with: {e}')
        if save:
            ssv.save_skeleton()
        if dest_path is not None:
            ssv.save_skeleton_to_kzip(dest_path=dest_path)


def map_myelin2coords(coords: np.ndarray,
                      cube_edge_avg: np.ndarray = np.array([21, 21, 11]),
                      thresh_proba: float = 255 // 2, thresh_majority: float = 0.1,
                      mag: int = 1) -> np.ndarray:
    """
    Retrieves a myelin prediction at every location in `coords`. The classification
    is the majority label within a cube of size `cube_edge_avg` around the
    respective location. Voxel-wise myelin predictions are found by thresholding the
    probability for myelinated voxels at `thresh` stored in the KnossosDataset at
    ``global_params.config.working_dir+'/knossosdatasets/myelin/'``.

    Examples:

        The entire myelin prediction for a single cell reconstruction including a smoothing
        via :func:`~majorityvote_skeleton_property` is implemented as follows::

            from syconn import global_params
            from syconn.reps.super_segmentation import *
            from syconn.reps.super_segmentation_helper import map_myelin2coords, majorityvote_skeleton_property

            # init. example data set
            global_params.wd = '~/SyConn/example_cube1/'

            # initialize example cell reconstruction
            ssd = SuperSegmentationDataset()
            ssv = list(ssd.ssvs)[0]
            ssv.load_skeleton()

            # get myelin predictions
            myelinated = map_myelin2coords(ssv.skeleton["nodes"], mag=4)
            ssv.skeleton["myelin"] = myelinated
            # this will generate a smoothed version at ``ssv.skeleton["myelin_avg10000"]``
            majorityvote_skeleton_property(ssv, "myelin")
            # store results as a KNOSSOS readable k.zip file
            ssv.save_skeleton_to_kzip(dest_path='~/{}_myelin.k.zip'.format(ssv.id),
                additional_keys=['myelin', 'myelin_avg10000'])

    Args:
        coords: Coordinates used to retrieve myelin predictions. In voxel coordinates (``mag=1``).
        cube_edge_avg: Cube size used for averaging myelin predictions for each location.
            The loaded data cube will always have the extent given by `cube_edge_avg`, regardless
            of the value of `mag`.
        thresh_proba: Classification threshold in uint8 values (0..255).
        thresh_majority: Majority ratio for myelin (between 0..1), i.e.
            ``thresh_majority=0.1`` means that 10% myelin voxels within ``cube_edge_avg``
            will flag the corresponding locations as myelinated.
        mag: Data mag. level used to retrieve the prediction results.

    Returns:
        Myelin prediction (0: no myelin, 1: myelinated neuron) at every coordinate.
    """
    myelin_kd_p = global_params.config.working_dir + "/knossosdatasets/myelin/"
    if not os.path.isdir(myelin_kd_p):
        raise ValueError(f'Could not find myelin KnossosDataset at {myelin_kd_p}.')
    kd = kd_factory(myelin_kd_p)
    myelin_preds = np.zeros((len(coords)), dtype=np.uint8)
    n_cube_vx = np.prod(cube_edge_avg)
    # convert to mag 1, TODO: requires adaption if anisotropic downsampling was used in KD!
    cube_edge_avg = cube_edge_avg * mag
    for ix, c in enumerate(coords):
        offset = c - cube_edge_avg // 2
        myelin_proba = kd.load_raw(size=cube_edge_avg, offset=offset, mag=mag).swapaxes(0, 2)
        myelin_ratio = np.sum(myelin_proba > thresh_proba) / n_cube_vx
        myelin_preds[ix] = myelin_ratio > thresh_majority
    return myelin_preds


# New Implementation of skeleton generation which makes use of ssv.rag
def from_netkx_to_arr(skel_nx: nx.Graph) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """

    Args:
        skel_nx:

    Returns:

    """
    skeleton = {}
    skeleton['nodes'] = np.array(
        [skel_nx.node[ix]['position'] for ix in skel_nx.nodes()],
        dtype=np.uint32)
    skeleton['diameters'] = np.zeros(len(skeleton['nodes']), dtype=np.float32)

    # Important bit, please don't remove (needed after pruning)
    # This transforms the edge values to contiguous node indices
    temp_edges = np.array(list(skel_nx.edges())).reshape(-1)
    temp_edges_sorted = np.unique(np.sort(temp_edges))
    temp_edges_dict = {}

    for ii, ix in enumerate(temp_edges_sorted):
        temp_edges_dict[ix] = ii

    temp_edges = [temp_edges_dict[ix] for ix in temp_edges]

    temp_edges = np.array(temp_edges, dtype=np.uint).reshape([-1, 2])
    skeleton['edges'] = temp_edges

    return skeleton['nodes'], skeleton['diameters'], skeleton['edges']


def sparsify_skeleton_fast(g: nx.Graph, scal: Optional[np.ndarray] = None,
                           dot_prod_thresh: float = 0.8,
                           max_dist_thresh: Union[int, float] = 500,
                           min_dist_thresh: Union[int, float] = 50,
                           verbose: bool = False) -> nx.Graph:
    """
    Reduces nodes in the skeleton.

    Args:
        g: networkx graph of the sso skel.
        scal: Scale factor; equal to the physical voxel size (nm).
        dot_prod_thresh: the 'straightness' of the edges.
        max_dist_thresh: Maximum distance desired between every node.
        min_dist_thresh: Minimum distance desired between every node.
        verbose: Log additional output.

    Returns: sso containing the sparse skeleton.

    """

    start = time.time()
    skel_nx = nx.Graph(g)
    n_nodes_start = skel_nx.number_of_nodes()
    if scal is None:
        scal = global_params.config['scaling']
    change = 1

    while change > 0:
        change = 0
        visiting_nodes = list({k for k, v in dict(skel_nx.degree()).items() if v == 2})
        for visiting_node in visiting_nodes:
            neighbours = [n for n in skel_nx.neighbors(visiting_node)]
            if skel_nx.degree(visiting_node) == 2:
                left_node = neighbours[0]
                right_node = neighbours[1]
                vector_left_node = np.array(
                    [int(skel_nx.node[left_node]['position'][ix]) - int(skel_nx.node[visiting_node]['position'][ix]) for
                     ix in range(3)]) * scal
                vector_right_node = np.array([int(skel_nx.node[right_node]['position'][ix]) -
                                              int(skel_nx.node[visiting_node]['position'][ix]) for ix in
                                              range(3)]) * scal

                dot_prod = np.dot(vector_left_node / np.linalg.norm(vector_left_node),
                                  vector_right_node / np.linalg.norm(vector_right_node))
                dist = np.linalg.norm([int(skel_nx.node[right_node]['position'][ix] * scal[ix]) - int(
                    skel_nx.node[left_node]['position'][ix] * scal[ix]) for ix in range(3)])

                if (abs(dot_prod) > dot_prod_thresh and dist < max_dist_thresh) or dist <= min_dist_thresh:
                    skel_nx.remove_node(visiting_node)
                    skel_nx.add_edge(left_node, right_node)
                    change += 1
    if verbose:
        log_reps.debug(f'sparsening took {time.time() - start}. Reduced {n_nodes_start} to '
                       f'{skel_nx.number_of_nodes()} nodes')
    return skel_nx


def create_new_skeleton_sv_fast(args):
    """
    Create a sparse supervoxel skeleton. Initial skeleton must exist.
    Similar to :func:`~create_new_skeleton` but used as multi-process helper
    method in :func:`~from_sso_to_netkx_fast`.

    Args:
        args: Supervoxel ID (int) and sparse flag (bool).

    Returns:
        Three arrays: Node coordinates [in voxels], diameter estimation per node,
        edges.
    """
    so_id, sparsify = args
    so = SegmentationObject(obj_type="sv", obj_id=so_id)
    so.enable_locking = False
    so.load_attr_dict()
    # ignore diameters, will be populated at the and of create_sso_skeleton_fast
    skel = load_skeleton(so)
    nodes, diameters, edges = skel['nodes'].astype(np.uint32), skel['diameters'], skel['edges']
    # create nx graph
    skel_nx = nx.Graph()
    skel_nx.add_nodes_from([(ix, dict(position=coord)) for ix, coord
                            in enumerate(nodes)])

    new_edges = [tuple(ix) for ix in edges]
    skel_nx.add_edges_from(new_edges)
    if sparsify:
        skel_nx = sparsify_skeleton_fast(skel_nx)
    n_cc = nx.number_connected_components(skel_nx)
    if n_cc > 1:
        log_reps.warning('SV {} contained {} connected components in its skel'
                         'eton representation. Stitching now.'
                         ''.format(so.id, n_cc))
        # make edge values and node IDs contiguous prior to stitching
        temp_edges = np.array(skel_nx.edges()).reshape(-1)
        temp_nodes_sorted = np.array(np.sort(skel_nx.nodes()).reshape(-1))
        temp_edges_dict = {}
        for ii, ix in enumerate(temp_nodes_sorted):
            temp_edges_dict[ix] = ii
        temp_edges = [temp_edges_dict[ix] for ix in temp_edges]
        temp_edges = np.array(temp_edges, dtype=np.uint).reshape([-1, 2])
        skel_nx_tmp = nx.Graph()
        skel_nx_tmp.add_nodes_from([(temp_edges_dict[ix], skel_nx.node[ix]) for ix in
                                    skel_nx.nodes()])
        skel_nx_tmp.add_edges_from(temp_edges)
        skel_nx = stitch_skel_nx(skel_nx_tmp)
    nodes, diameters, edges = from_netkx_to_arr(skel_nx)
    # just get nodes, diameters and edges
    return nodes, diameters, edges


def from_sso_to_netkx_fast(sso, sparsify=True, max_edge_length=1.5e3):
    """
    Stitches the SV skeletons using the supervoxel graph ``sso.rag``.

    Args:
        sso: SuperSegmentationObject
        sparsify: bool
            Sparsify SV skeletons before stitching
        max_edge_length: float
            Maximum edge length in nanometers.

    Returns: nx.Graph


    """
    skel_nx = nx.Graph()
    sso.load_attr_dict()
    ssv_skel = {'nodes': [], 'edges': [], 'diameters': []}
    res = start_multiprocess_imap(create_new_skeleton_sv_fast,
                                  [(sv_id, sparsify) for sv_id in sso.sv_ids],
                                  nb_cpus=sso.nb_cpus, show_progress=False,
                                  debug=False)
    nodes, diameters, edges, sv_id_arr = [], [], [], []
    # first offset is 0, last length is not needed
    n_nodes_per_sv = [0] + list(
        np.cumsum([len(el[0]) for el in res])[:-1])

    for ii in range(len(res)):
        if len(res[ii][0]) == 0:  # skip missing / empty skeletons, e.g. happens for small SVs
            continue
        nodes.append(res[ii][0])
        diameters.append(res[ii][1])
        edges.append(res[ii][2] + int(n_nodes_per_sv[ii]))
        # store mapping from node to SV ID
        sv_id_arr.append([sso.sv_ids[ii]] * len(res[ii][0]))

    ssv_skel['nodes'] = np.concatenate(nodes)
    ssv_skel['diameters'] = np.concatenate(diameters, axis=0)
    sv_id_arr = np.concatenate(sv_id_arr)
    node_ix_arr = np.arange(len(sv_id_arr))

    added_edges = set()
    # stitching
    if len(sso.sv_ids) > 1:
        # iterates over SV object edges
        g = sso.load_sv_graph()
        # # TODO: activate as soon as SV graphs only connect adjacent SVs.
        # # bridge SVs without skeleton > might speed up the SSV
        # # skeleton generation if they lead to splits in the SV-graph -> fallback
        # # to `stitch_skel_nx` is slow.
        # # copy edges, as those might be modified inside loop
        # for e1, e2 in list(g.edges()):
        #     # get closest node-pair between SV nodes in question and add new edge
        #     nodes1 = ssv_skel['nodes'][sv_id_arr == e1.id] * sso.scaling
        #     nodes2 = ssv_skel['nodes'][sv_id_arr == e2.id] * sso.scaling
        #     nodes1 = nodes1.astype(np.float32)
        #     nodes2 = nodes2.astype(np.float32)
        #     if len(nodes1) == 0:
        #         # bridge SV without skeleton
        #         neighbors = g.neighbors(e1)
        #         for p in itertools.combinations(neighbors, 2):
        #             g.add_edge(p[0], p[1])
        #     if len(nodes2) == 0:
        #         # bridge SV without skeleton
        #         neighbors = g.neighbors(e2)
        #         for p in itertools.combinations(neighbors, 2):
        #             g.add_edge(p[0], p[1])

        for e1, e2 in g.edges():
            # get closest node-pair between SV nodes in question and add new edge
            nodes1 = ssv_skel['nodes'][sv_id_arr == e1.id] * sso.scaling
            nodes2 = ssv_skel['nodes'][sv_id_arr == e2.id] * sso.scaling
            nodes1 = nodes1.astype(np.float32)
            nodes2 = nodes2.astype(np.float32)
            if len(nodes1) == 0 or len(nodes2) == 0:
                continue  # SV without skeleton
            nodes1_ix = node_ix_arr[sv_id_arr == e1.id]
            nodes2_ix = node_ix_arr[sv_id_arr == e2.id]
            tree = spatial.cKDTree(nodes1)
            dists, node_ixs1 = tree.query(nodes2)
            # # get global index of nodes
            ix2 = nodes2_ix[np.argmin(dists)]
            ix1 = nodes1_ix[node_ixs1[np.argmin(dists)]]
            added_edges.add((sv_id_arr[ix1], sv_id_arr[ix2]))
            node_dist_check = np.linalg.norm(ssv_skel['nodes'][ix1].astype(np.float32) *
                                             sso.scaling - ssv_skel['nodes'][ix2].astype(
                np.float32) * sso.scaling)
            if np.min(dists) < node_dist_check or node_dist_check > max_edge_length:
                log_reps.debug(f'Found long edge with length '
                               f'{node_dist_check / 1e3:.0f} um between SVs '
                               f'{e1.id} and {e2.id} although they were '
                               f'connected within the SV graph. Skipping.')
                # TODO: remove as soon as SV graphs only connect adjacent SVs.
                continue
            edges.append(np.array([[ix1, ix2]], dtype=np.uint32))
    ssv_skel['edges'] = np.concatenate(edges)

    if len(ssv_skel['nodes']) == 0:
        sso.skeleton = ssv_skel
        return
    skel_nx.add_nodes_from([(ix, dict(position=coord)) for ix, coord
                            in enumerate(ssv_skel['nodes'])])
    edges = [tuple(ix) for ix in ssv_skel['edges']]
    skel_nx.add_edges_from(edges)
    if nx.number_connected_components(skel_nx) != 1:
        msg = 'Stitching of SV skeletons failed during "from_sso_to_netkx_' \
              'fast" with {} connected components using the underlying SSV ' \
              'agglomeration. Please check the underlying RAG of SSV {}. ' \
              'Now performing a slower stitching method to add missing edg' \
              'es recursively between the closest connected components. ' \
              'This warning might also occur if two supervoxels are connected ' \
              'over supervoxel(s) without skeleton!' \
              ''.format(nx.number_connected_components(skel_nx), sso.id)
        skel_nx = stitch_skel_nx(skel_nx)
        log_reps.warning(msg)
        assert nx.number_connected_components(skel_nx) == 1
        ssv_skel['edges'] = np.array(skel_nx.edges(), dtype=np.uint)
    sso.skeleton = ssv_skel
    return skel_nx


def create_sso_skeleton_fast(sso, pruning_thresh=800, sparsify=True, max_dist_thresh=600, dot_prod_thresh=0.0,
                             max_dist_thresh_iter2=600):
    """
    Creates the super-supervoxel skeleton. NOTE: If the underlying RAG does
    not connect close-by SVs it will be slower than :func:`~create_sso_skeleton`.
    The latter will recursively add the shortest edge between two different SVs.
    This method will add edges between supervoxels which are connected in the
    supervoxel graph. To use multi-processing set ``ssv.nb_cpus`` > 1.

    Args:
        sso: Super Segmentation Object
        pruning_thresh: int
            Threshold for pruning step (in NM). Removes branches below this path
            length, see :func:`prune_stub_branches`.
        sparsify: bool
            will sparsify if True otherwise not
        max_dist_thresh: float
            Maximum distance in NM of two adjacent edges in order to prune the node
            in-between. Initial sparsening via :func:`~skeleton_optimization`.
        dot_prod_thresh: float
            Dot product value of two adjacent edges. Above this value, the node
            in-between will be pruned. Used in :func:`~sparsify_skeleton_fast` after
            first sparsening and pruning.
        max_dist_thresh_iter2: float
            Maximum distance in NM of two adjacent edges in order to prune the node
            in-between. Used in :func:`~sparsify_skeleton_fast` after
            first sparsening and pruning.


    Returns:
        The cell reconstruction with sparse skeleton (as MST) and radius
        estimates.

    """
    # Creating network kx graph from sso skel
    # log_reps.debug('Creating skeleton of SSO {}'.format(sso.id))
    skel_nx = from_sso_to_netkx_fast(sso)
    # log_reps.debug('Number CC after stitching and sparsifying SSO {}: {}'.format(sso.id,
    #       nx.number_connected_components(skel_nx)))
    # Sparse again after stitching. Inexpensive.
    if sparsify:
        skel_nx = sparsify_skeleton_fast(skel_nx, max_dist_thresh=max_dist_thresh,
                                         min_dist_thresh=max_dist_thresh)

        # log_reps.debug(
        #     'Number CC after 2nd sparsification SSO {}: {}'.format(sso.id,
        #     nx.number_connected_components(skel_nx)))
    # Pruning the stitched sso skeletons

    _, skel_nx = prune_stub_branches(nx_g=skel_nx, len_thres=pruning_thresh)
    if sparsify:
        # dot_prod_thresh=0.95: this allows to remove nodes which neighboring
        # edges have an angle below ~36°
        skel_nx = sparsify_skeleton_fast(
            skel_nx, dot_prod_thresh=dot_prod_thresh,
            max_dist_thresh=max_dist_thresh_iter2)
    start = time.time()
    for e in skel_nx.edges:
        w = np.linalg.norm(
            (skel_nx.node[e[0]]['position'] - skel_nx.node[e[1]]['position']) * global_params.config['scaling'])
        skel_nx[e[0]][e[1]]['weight'] = w
    skel_nx = nx.minimum_spanning_tree(skel_nx)
    log_reps.debug(f'mst took {time.time() - start:.0f} s')
    sso = from_netkx_to_sso(sso, skel_nx)
    # reset weighted graph
    sso._weighted_graph = None
    # log_reps.debug('Number CC after pruning SSO {}: {}'.format(sso.id,
    #       nx.number_connected_components(skel_nx)))
    # Estimating the radii
    start = time.time()
    sso.skeleton = radius_correction_found_vertices(sso)
    log_reps.debug(f'radius estimation took {time.time() - start:.0f} s')

    return sso


def glia_pred_exists(so):
    so.load_attr_dict()
    return "glia_probas" in so.attr_dict


def views2tripletinput(views):
    views = views[:, :, :1]  # use first view only
    out_d = np.concatenate([views,
                            np.ones_like(views),
                            np.ones_like(views)], axis=2)
    return out_d.astype(np.float32)


def get_pca_view_hists(sso, t_net, pca):
    views = sso.load_views()
    latent = t_net.predict_proba(views2tripletinput(views))
    latent = pca.transform(latent)
    hist0 = np.histogram(latent[:, 0], bins=50, range=[-2, 2], normed=True)
    hist1 = np.histogram(latent[:, 1], bins=50, range=[-3.2, 3], normed=True)
    hist2 = np.histogram(latent[:, 2], bins=50, range=[-3.5, 3.5], normed=True)
    return np.array([hist0, hist1, hist2])


def save_view_pca_proj(sso, t_net, pca, dest_dir, ls=20, s=6.0, special_points=(),
                       special_markers=(), special_kwargs=()):
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    views = sso.load_views()
    latent = t_net.predict_proba(views2tripletinput(views))
    latent = pca.transform(latent)
    col = (np.array(latent) - latent.min(axis=0)) / (latent.max(axis=0) - latent.min(axis=0))
    col = np.concatenate([col, np.ones_like(col)[:, :1]], axis=1)
    for ii, (a, b) in enumerate([[0, 1], [0, 2], [1, 2]]):
        fig, ax = plt.subplots()
        plt.scatter(latent[:, a], latent[:, b], c=col, s=s, lw=0.5, marker="o",
                    edgecolors=col)
        if len(special_points) >= 0:
            for kk, sp in enumerate(special_points):
                if len(special_markers) == 0:
                    sm = "x"
                else:
                    sm = special_markers[kk]
                if len(special_kwargs) == 0:
                    plt.scatter(sp[None, a], sp[None, b], s=75.0, lw=2.3,
                                marker=sm, edgecolor="0.3", facecolor="none")
                else:
                    plt.scatter(sp[None, a], sp[None, b], **special_kwargs)
        fig.patch.set_facecolor('white')
        ax.tick_params(axis='x', which='major', labelsize=ls, direction='out',
                       length=4, width=3, right="off", top="off", pad=10)
        ax.tick_params(axis='y', which='major', labelsize=ls, direction='out',
                       length=4, width=3, right="off", top="off", pad=10)

        ax.tick_params(axis='x', which='minor', labelsize=ls, direction='out',
                       length=4, width=3, right="off", top="off", pad=10)
        ax.tick_params(axis='y', which='minor', labelsize=ls, direction='out',
                       length=4, width=3, right="off", top="off", pad=10)
        plt.xlabel(r"$Z_%d$" % (a + 1), fontsize=ls)
        plt.ylabel(r"$Z_%d$" % (b + 1), fontsize=ls)
        ax.xaxis.set_major_locator(ticker.MultipleLocator(2))
        ax.yaxis.set_major_locator(ticker.MultipleLocator(2))
        plt.tight_layout()
        plt.savefig(dest_dir + "/%d_pca_%d%d.png" % (sso.id, a + 1, b + 1), dpi=400)
        plt.close()


def extract_skel_features(ssv, feature_context_nm=8000, max_diameter=1000,
                          obj_types=("sj", "mi", "vc"), downsample_to=None):
    """

    Args:
        ssv:
        feature_context_nm: int
            effective field for feature statistic 2*feature_context_nm
        max_diameter:
        obj_types:
        downsample_to:

    Returns:

    """
    node_degrees = np.array(list(dict(ssv.weighted_graph().degree()).values()),
                            dtype=np.int)

    sizes = {}
    for obj_type in obj_types:
        objs = ssv.get_seg_objects(obj_type)
        sizes[obj_type] = np.array([obj.size for obj in objs],
                                   dtype=np.int)

    if downsample_to is not None:
        if downsample_to > len(ssv.skeleton["nodes"]):
            downsample_by = 1
        else:
            downsample_by = int(len(ssv.skeleton["nodes"]) /
                                float(downsample_to))
    else:
        downsample_by = 1

    features = []
    for i_node in range(len(ssv.skeleton["nodes"][::downsample_by])):
        this_i_node = i_node * downsample_by
        this_features = []

        paths = nx.single_source_dijkstra_path(ssv.weighted_graph(),
                                               this_i_node,
                                               feature_context_nm)
        neighs = np.array(list(paths.keys()), dtype=np.int)

        neigh_diameters = ssv.skeleton["diameters"][neighs]
        this_features.append(np.mean(neigh_diameters))
        this_features.append(np.std(neigh_diameters))
        hist_feat = np.histogram(neigh_diameters, bins=10, range=(0, max_diameter))[0]
        hist_feat = np.array(hist_feat) / hist_feat.sum()
        this_features += list(hist_feat)
        this_features.append(np.mean(node_degrees[neighs]))

        for obj_type in obj_types:
            neigh_objs = np.array(ssv.skeleton["assoc_%s" % obj_type])[
                neighs]
            neigh_objs = [item for sublist in neigh_objs for item in
                          sublist]
            neigh_objs = np.unique(np.array(neigh_objs))
            if len(neigh_objs) == 0:
                this_features += [0, 0, 0]
                continue

            this_features.append(len(neigh_objs))
            obj_sizes = sizes[obj_type][neigh_objs]
            this_features.append(np.mean(obj_sizes))
            this_features.append(np.std(obj_sizes))

        # box feature
        edge_len = feature_context_nm * 2
        bb = [ssv.skeleton["nodes"][this_i_node], np.array([edge_len, ] * 3)]
        vol_tot = feature_context_nm ** 3
        node_density = np.sum(in_bounding_box(ssv.skeleton["nodes"], bb)) / vol_tot
        this_features.append(node_density)

        features.append(np.array(this_features))
    return np.array(features)


def associate_objs_with_skel_nodes(ssv, obj_types=("sj", "vc", "mi"),
                                   downsampling=(8, 8, 4)):
    if ssv.skeleton is None:
        ssv.load_skeleton()

    for obj_type in obj_types:
        voxels = []
        voxel_ids = [0]
        for obj in ssv.get_seg_objects(obj_type):
            vl = obj.load_voxel_list_downsampled_adapt(downsampling)

            if len(vl) == 0:
                continue

            if len(voxels) == 0:
                voxels = vl
            else:
                voxels = np.concatenate((voxels, vl))

            voxel_ids.append(voxel_ids[-1] + len(vl))

        if len(voxels) == 0:
            ssv.skeleton["assoc_%s" % obj_type] = [[]] * len(
                ssv.skeleton["nodes"])
            continue

        voxel_ids = np.array(voxel_ids)

        kdtree = scipy.spatial.cKDTree(voxels * ssv.scaling)
        balls = kdtree.query_ball_point(ssv.skeleton["nodes"] *
                                        ssv.scaling, 500)
        nodes_objs = []
        for i_node in range(len(ssv.skeleton["nodes"])):
            nodes_objs.append(list(np.unique(
                np.sum(voxel_ids[:, None] <= np.array(balls[i_node]),
                       axis=0) - 1)))

        ssv.skeleton["assoc_%s" % obj_type] = nodes_objs

    ssv.save_skeleton(to_kzip=False, to_object=True)


def skelnode_comment_dict(sso):
    comment_dict = {}
    skel = load_skeleton_kzip(sso.skeleton_kzip_path)["skeleton"]
    for n in skel.getNodes():
        c = frozenset(n.getCoordinate())
        comment_dict[c] = n.getComment()
    return comment_dict


def label_array_for_sso_skel(sso, comment_converter):
    """
    Converts skeleton node comments from annotation.xml in
    sso.skeleton_kzip_path (see SkeletonAnnotation class from knossos utils)
    to a label array of length (and same ordering) as sso.skeleton["nodes"].
    If comment was unspecified, it will get label -1

    Args:
        sso: SuperSegmentationObject
        comment_converter: dict
            Key: Comment, Value: integer label
    Returns: np.array
        Label array of len(sso.skeleton["nodes"])

    """
    if sso.skeleton is None:
        sso.load_skeleton()
    cd = skelnode_comment_dict(sso)
    label_array = np.ones(len(sso.skeleton["nodes"]), dtype=np.int) * -1
    for ii, n in enumerate(sso.skeleton["nodes"]):
        comment = cd[frozenset(n.astype(np.int))].lower()
        try:
            label_array[ii] = comment_converter[comment]
        except KeyError:
            pass
    return label_array


def write_axpred_cnn(ssv, pred_key_appendix, dest_path=None, k=1):
    if dest_path is None:
        dest_path = ssv.skeleton_kzip_path_views
    pred_key = "axoness_preds%s" % pred_key_appendix
    if not ssv.attr_exists(pred_key):
        log_reps.info("Couldn't find specified axoness prediction. Falling back "
                      "to default.")
        preds = np.array(start_multiprocess_obj("axoness_preds",
                                                [[sv, {
                                                    "pred_key_appendix": pred_key_appendix}]
                                                 for sv in ssv.svs],
                                                nb_cpus=ssv.nb_cpus))
        preds = np.concatenate(preds)
    else:
        preds = ssv.lookup_in_attribute_dict(pred_key)
    log_reps.debug("Collected axoness: {}".format(Counter(preds).most_common()))
    locs = ssv.sample_locations()
    log_reps.debug("Collected locations.")
    pred_coords = np.concatenate(locs)
    assert pred_coords.ndim == 2
    assert pred_coords.shape[1] == 3
    colors = np.array(np.array([[0.6, 0.6, 0.6, 1], [0.841, 0.138, 0.133, 1.],
                                [0.32, 0.32, 0.32, 1.]]) * 255, dtype=np.uint)
    ssv._pred2mesh(pred_coords, preds, "axoness.ply", dest_path=dest_path, k=k,
                   colors=colors)


def cnn_axoness2skel(sso: 'super_segmentation.SuperSegmentationObject',
                     pred_key_appendix: str = "", k: int = 1,
                     force_reload: bool = False,
                     save_skel: bool = True, use_cache: bool = False):
    """
    By default, will create 'axoness_preds_cnn' attribute in SSV attribute dict
    and save new skeleton attributes with keys "axoness" and "axoness_probas".

    Args:
        sso: SuperSegmentationObject
        pred_key_appendix: str
        k: int
        force_reload: bool
            Reload SV predictions.
        save_skel: bool
            Save SSV skeleton with prediction attirbutes
        use_cache: bool
            Write intermediate SV predictions in SSV attribute dict to disk
    Returns:

    """
    if k != 1:
        log_reps.warn("Parameter 'k' is deprecated but was set to {}. "
                      "It is not longer used in this method.".format(k))
    if sso.skeleton is None:
        sso.load_skeleton()
    proba_key = "axoness_probas_cnn%s" % pred_key_appendix
    pred_key = "axoness_preds_cnn%s" % pred_key_appendix
    if not sso.attr_exists(pred_key) or not sso.attr_exists(proba_key) or \
            force_reload:
        preds = np.array(start_multiprocess_obj(
            "axoness_preds", [[sv, {"pred_key_appendix": pred_key_appendix}]
                              for sv in sso.svs],
            nb_cpus=sso.nb_cpus))
        probas = np.array(start_multiprocess_obj(
            "axoness_probas", [[sv, {"pred_key_appendix": pred_key_appendix}]
                               for sv in sso.svs], nb_cpus=sso.nb_cpus))
        preds = np.concatenate(preds)
        probas = np.concatenate(probas)
        sso.attr_dict[proba_key] = probas
        sso.attr_dict[pred_key] = preds
        if use_cache:
            sso.save_attributes([proba_key, pred_key], [probas, preds])
    else:
        preds = sso.lookup_in_attribute_dict(pred_key)
        probas = sso.lookup_in_attribute_dict(proba_key)
    loc_coords = np.concatenate(sso.sample_locations())
    assert len(loc_coords) == len(preds), "Number of view coordinates is" \
                                          "different from number of view" \
                                          "predictions. SSO %d" % sso.id
    # find NN in loc_coords for every skeleton node and use their majority
    # prediction
    node_preds = colorcode_vertices(sso.skeleton["nodes"] * sso.scaling,
                                    loc_coords, preds, colors=[0, 1, 2], k=1)
    node_probas, ixs = assign_rep_values(sso.skeleton["nodes"] * sso.scaling,
                                         loc_coords, probas, return_ixs=True)
    assert np.max(ixs) <= len(loc_coords), "Maximum index for sample " \
                                           "coordinates is bigger than " \
                                           "length of sample coordinates."
    sso.skeleton["axoness%s" % pred_key_appendix] = node_preds
    sso.skeleton["axoness_probas%s" % pred_key_appendix] = node_probas
    sso.skeleton["view_ixs"] = ixs
    if save_skel:
        sso.save_skeleton()


def average_node_axoness_views(sso: 'super_segmentation.SuperSegmentationObject',
                               pred_key: Optional[str] = None,
                               pred_key_appendix: str = "",
                               max_dist: int = 10000, return_res: bool = False,
                               use_cache: bool = False):
    """
    Averages the axoness prediction along skeleton with maximum path length
    of 'max_dist'. Therefore, view indices were mapped to every skeleton
    node and collected while traversing the skeleton. The majority of the
    set of their predictions will be assigned to the source node.
    By default, will create 'axoness_preds_cnn' attribute in SSV attribute dict
    and save new skeleton attribute with key
    ``"%s_views_avg%d" % (pred_key, max_dist)``. This method will not call
    ``sso.save_skeleton()``.

    Args:
        sso:
        pred_key: Key for the stored SV predictions.
        pred_key_appendix: If `pred_key` is None, it will be set to
            ``pred_key = "axoness_preds_cnn%s" % pred_key_appendix``
        max_dist:
        return_res:
        use_cache: Write intermediate SV predictions in SSV attribute dict
            to disk.

    Returns:

    """
    if sso.skeleton is None:
        sso.load_skeleton()
    if len(sso.skeleton["edges"]) == 0:
        log_reps.error("Zero edges in skeleton of SSV %d. "
                       "Skipping averaging." % sso.id)
        return
    if pred_key is None:
        pred_key = "axoness_preds_cnn%s" % pred_key_appendix
    elif len(pred_key_appendix) > 0:
        raise ValueError("Only one of the two may be given: 'pred_key' or"
                         "'pred_key_appendix', but not both.")
    if type(pred_key) != str:
        raise ValueError("'pred_key' has to be of type str.")
    if not sso.attr_exists(pred_key) and ("axoness_preds_cnn" not in pred_key):
        if len(pred_key_appendix) > 0:
            log_reps.error("Couldn't find specified axoness prediction. Falling"
                           " back to default (-> per SV stored multi-view "
                           "prediction including SSV context")
        preds = np.array(start_multiprocess_obj(
            "axoness_preds", [[sv, {"pred_key_appendix": pred_key_appendix}]
                              for sv in sso.svs], nb_cpus=sso.nb_cpus))
        preds = np.concatenate(preds)
        sso.attr_dict[pred_key] = preds
        if use_cache:
            sso.save_attributes([pred_key], [preds])
    else:
        preds = sso.lookup_in_attribute_dict(pred_key)
    loc_coords = np.concatenate(sso.sample_locations())
    assert len(loc_coords) == len(preds), "Number of view coordinates is " \
                                          "different from number of view " \
                                          "predictions. SSO %d" % sso.id
    if "view_ixs" not in sso.skeleton.keys():
        log_reps.info("View indices were not yet assigned to skeleton nodes. "
                      "Running now '_cnn_axonness2skel(sso, "
                      "pred_key_appendix=pred_key_appendix, k=1)'")
        cnn_axoness2skel(sso, pred_key_appendix=pred_key_appendix, k=1,
                         save_skel=not return_res, use_cache=use_cache)
    view_ixs = np.array(sso.skeleton["view_ixs"])
    avg_pred = []

    g = sso.weighted_graph()
    for n in range(g.number_of_nodes()):
        paths = nx.single_source_dijkstra_path(g, n, max_dist)
        neighs = np.array(list(paths.keys()), dtype=np.int)
        unique_view_ixs = np.unique(view_ixs[neighs], return_counts=False)
        cls, cnts = np.unique(preds[unique_view_ixs], return_counts=True)
        c = cls[np.argmax(cnts)]
        avg_pred.append(c)
    if return_res:
        return avg_pred
    sso.skeleton["axoness%s_avg%d" % (pred_key_appendix, max_dist)] = avg_pred


def majority_vote_compartments(sso, ax_pred_key='axoness'):
    """
    By default, will save new skeleton attribute with key
    ax_pred_key + "_comp_maj". Will not call ``sso.save_skeleton()``.

    Args:
        sso: SuperSegmentationObject
        ax_pred_key: str
            Key for the axoness predictions stored in sso.skeleton

    Returns:

    """
    g = sso.weighted_graph(add_node_attr=(ax_pred_key,))
    soma_free_g = g.copy()
    for n, d in g.nodes(data=True):
        if d[ax_pred_key] == 2:
            soma_free_g.remove_node(n)
    ccs = list(nx.connected_component_subgraphs(soma_free_g))
    new_axoness_dc = nx.get_node_attributes(g, ax_pred_key)
    for cc in ccs:
        preds = [d[ax_pred_key] for n, d in cc.nodes(data=True)]
        cls, cnts = np.unique(preds, return_counts=True)
        majority = cls[np.argmax(cnts)]
        probas = np.array(cnts, dtype=np.float32) / np.sum(cnts)
        # positively bias dendrite assignment
        if (majority == 1) and (probas[cls == 1] < 0.66):
            majority = 0
        for n in cc.nodes():
            new_axoness_dc[n] = majority
    nx.set_node_attributes(g, new_axoness_dc, ax_pred_key)
    new_axoness_arr = np.zeros((len(sso.skeleton["nodes"])))
    for n, d in g.nodes(data=True):
        new_axoness_arr[n] = d[ax_pred_key]
    sso.skeleton[ax_pred_key + "_comp_maj"] = new_axoness_arr
    sso.save_skeleton()


def majorityvote_skeleton_property(sso: 'super_segmentation.SuperSegmentationObject',
                                   prop_key: str, max_dist: int = 10000,
                                   return_res: bool = False) -> np.ndarray:
    """
    Applies a sliding window majority vote along the skeleton of a given
    :class:`~syconn.reps.super_segmentation_object.SuperSegmentationObject`.
    Will not call ``sso.save_skeleton()``.

    Args:
        sso: The cell reconstruction object.
        prop_key: Key of the property which will be processed.
        max_dist: Maximum traversal distance along L2-distance weighted graph.
        return_res: If True, majority result will be returned.

    Returns:
        The majority vote of the requested property.
    """
    if not prop_key in sso.skeleton:
        raise ValueError(f'Given property "{prop_key}" does not exist in '
                         f'skeleton of SSV {sso.id}.')
    g = sso.weighted_graph()
    avg_prop = []
    for n in range(g.number_of_nodes()):
        paths = nx.single_source_dijkstra_path(g, n, max_dist)
        neighs = np.array(list(paths.keys()), dtype=np.int)
        prop_vals, cnts = np.unique(sso.skeleton[prop_key][neighs],
                                    return_counts=True)
        c = prop_vals[np.argmax(cnts)]
        avg_prop.append(c)
    avg_prop = np.array(avg_prop)
    if return_res:
        return avg_prop
    sso.skeleton["%s_avg%d" % (prop_key, max_dist)] = avg_prop


def find_incomplete_ssv_views(ssd, woglia, n_cores=global_params.config['ncores_per_node']):
    sd = ssd.get_segmentationdataset("sv")
    incomplete_sv_ids = find_missing_sv_views(sd, woglia, n_cores)
    missing_ssv_ids = set()
    for sv_id in incomplete_sv_ids:
        try:
            ssv_id = ssd.mapping_dict_reversed[sv_id]
            missing_ssv_ids.add(ssv_id)
        except KeyError:
            pass  # sv does not exist in this SSD
    return list(missing_ssv_ids)


def find_incomplete_ssv_skeletons(ssd, n_cores=global_params.config['ncores_per_node']):
    svs = np.concatenate([list(ssv.svs) for ssv in ssd.ssvs])
    incomplete_sv_ids = find_missing_sv_skeletons(svs, n_cores)
    missing_ssv_ids = set()
    for sv_id in incomplete_sv_ids:
        try:
            ssv_id = ssd.mapping_dict_reversed[sv_id]
            missing_ssv_ids.add(ssv_id)
        except KeyError:
            pass  # sv does not exist in this SSD
    return list(missing_ssv_ids)


def find_missing_sv_attributes_in_ssv(ssd, attr_key, n_cores=global_params.config['ncores_per_node']):
    sd = ssd.get_segmentationdataset("sv")
    incomplete_sv_ids = find_missing_sv_attributes(sd, attr_key, n_cores)
    missing_ssv_ids = set()
    for sv_id in incomplete_sv_ids:
        try:
            ssv_id = ssd.mapping_dict_reversed[sv_id]
            missing_ssv_ids.add(ssv_id)
        except KeyError:
            pass  # sv does not exist in this SSD
    return list(missing_ssv_ids)


def predict_views_semseg(views, model, batch_size=10, verbose=False):
    """
    Predicts a view array of shape [N_LOCS, N_CH, N_VIEWS, X, Y] with
    N_LOCS locations each with N_VIEWS perspectives, N_CH different channels
    (e.g. shape of cell, mitochondria, synaptic junctions and vesicle clouds).

    Args:
        views: np.array
            shape of [N_LOCS, N_CH, N_VIEWS, X, Y] as uint8 scaled from 0 to 255
        model: pytorch model
        batch_size: int
        verbose: bool

    Returns:

    """
    # if verbose:
    #     log_reps.debug('Reshaping view array with shape {}.'
    #                    ''.format(views.shape))
    views = views.astype(np.float32) / 255.
    views = views.swapaxes(1, 2)  # swap channel and view axis
    # N, 2, 4, 128, 256
    orig_shape = views.shape
    # reshape to predict single projections, N*2, 4, 128, 256
    views = views.reshape([-1] + list(orig_shape[2:]))

    # if verbose:
    #     log_reps.debug('Predicting view array with shape {}.'
    #                    ''.format(views.shape))
    # predict and reset to original shape: N, 2, 4, 128, 256
    labeled_views = model.predict_proba(views, bs=batch_size, verbose=verbose)
    labeled_views = np.argmax(labeled_views, axis=1)[:, None]
    # if verbose:
    #     log_reps.debug('Finished prediction of view array with shape {}.'
    #                    ''.format(views.shape))
    labeled_views = labeled_views.reshape(list(orig_shape[:2])
                                          + list(labeled_views.shape[1:]))
    # swap axes to get source shape
    labeled_views = labeled_views.swapaxes(2, 1)
    return labeled_views


def pred_svs_semseg(model, views, pred_key=None, svs=None, return_pred=False,
                    nb_cpus=1, verbose=False):
    """
    Predicts views of a list of SVs and saves them via SV.save_views.
    Efficient helper function for chunked predictions,
    therefore requires pre-loaded views.

    Args:
        model:
        views: List[np.array]
            N_SV each with np.array of shape [N_LOCS, N_CH, N_VIEWS, X, Y] as uint8 scaled from 0 to 255
        pred_key: str
        svs: Optional[list[SegmentationObject]]
        return_pred: Optional[bool]
        nb_cpus: int
            number CPUs for saving the SV views
        verbose: bool

    Returns: list[np.array]
        if 'return_pred=True' it returns the label views of input

    """
    if not return_pred and (svs is None or pred_key is None):
        raise ValueError('SV objects and "pred_key" have to be given if'
                         ' predictions should be saved at SV view storages.')
    part_views = np.cumsum([0] + [len(v) for v in views])
    assert len(part_views) == len(views) + 1
    views = np.concatenate(views)  # merge axis 0, i.e. N_SV and N_LOCS to N_SV*N_LOCS
    # views have shape: M, 4, 2, 128, 256
    label_views = predict_views_semseg(views, model, verbose=verbose)
    svs_labelviews = []
    for ii in range(len(part_views[:-1])):
        sv_label_views = label_views[part_views[ii]:part_views[ii + 1]]
        svs_labelviews.append(sv_label_views)
    assert len(part_views) == len(svs_labelviews) + 1
    if return_pred:
        return svs_labelviews
    params = [[sv, dict(views=views, index_views=False, woglia=True,
                        view_key=pred_key)]
              for sv, views in zip(svs, svs_labelviews)]
    start_multiprocess_obj('save_views', params, nb_cpus=nb_cpus)


def pred_sv_chunk_semseg(args):
    """
    Helper method to predict the 2D projects of supervoxels.

    Args:
        args: Paths to the supervoxel storages which are processed, model,
            supervoxel and prediction parameters.

    Returns:

    """

    from syconn.proc.sd_proc import sos_dict_fact, init_sos
    from syconn.backend.storage import CompressedStorage
    from syconn.handler.prediction import get_semseg_spiness_model
    so_chunk_paths = args[0]
    so_kwargs = args[1]
    pred_kwargs = args[2]

    # By default use views after glia removal
    if 'woglia' in pred_kwargs:
        woglia = pred_kwargs["woglia"]
        del pred_kwargs["woglia"]
    else:
        woglia = True
    pred_key = pred_kwargs["pred_key"]
    if 'raw_only' in pred_kwargs:
        raw_only = pred_kwargs['raw_only']
        del pred_kwargs['raw_only']
    else:
        raw_only = False

    model = get_semseg_spiness_model()
    for p in so_chunk_paths:
        # get raw views
        view_dc_p = p + "/views_woglia.pkl" if woglia else p + "/views.pkl"
        view_dc = CompressedStorage(view_dc_p, disable_locking=True)
        svixs = list(view_dc.keys())
        if len(svixs) == 0:
            continue
        views = list(view_dc.values())
        if raw_only:
            views = views[:, :1]
        sd = sos_dict_fact(svixs, **so_kwargs)
        svs = init_sos(sd)
        label_views = pred_svs_semseg(model, views, svs, return_pred=True,
                                      verbose=False)
        # choose any SV to get a path constructor for the v
        # iew storage (is the same for all SVs of this chunk)
        lview_dc_p = svs[0].view_path(woglia, view_key=pred_key)
        label_vd = CompressedStorage(lview_dc_p, disable_locking=True)
        for ii in range(len(svs)):
            label_vd[svs[ii].id] = label_views[ii]
        label_vd.push()


@jit(nopython=True)
def semseg2mesh_counter(index_arr: np.ndarray, label_arr: np.ndarray,
                        bg_label: int, count_arr: np.ndarray) -> np.ndarray:
    """
    Count the labels in `label_arr` of every ID in `index_arr`.

    Args:
        index_arr: Flat array of contiguous vertex IDs.
            Order must match `label_arr`.
        label_arr: Semantic segmentation prediction results as flat array.
            Order must match `index_arr`. Maximum value must be below `bg_label`.
        bg_label: Background label, will not be counted.
        count_arr: Zero-initialized array storing the per-vertex counted labels
            as given in `label_arr`. Must have shape (M, bg_label) where M is
            the number of vertices of the underyling mesh.

    Returns:
        Array filled with the per-vertex label counts.
    """
    for ii in range(len(index_arr)):
        vertex_ix = index_arr[ii]
        if vertex_ix == bg_label:
            continue
        l = label_arr[ii]  # vertex label
        count_arr[vertex_ix][l] += 1
    return count_arr


def semseg2mesh(sso, semseg_key, nb_views=None, dest_path=None, k=1,
                colors=None, force_recompute=False, index_view_key=None):
    """
    Maps semantic segmentation to SSV mesh.

    Notes:
        * ``k>0`` should only be used if a prediction for all vertices is
          absolutely required. Filtering of background and unpredicted vertices
          should be favored if time complexity is critical.

    Args:
        sso: The cell reconstruction.
        semseg_key: The key of the views which contain the semantic
            segmentation results, i.e. pixel-wise labels.
        index_view_key: Key of the views which hold the vertex indices at every
            pixel. If `index_view_key` is set, `nb_views` is ignored.
        nb_views: Number of views used for the prediction. Required for loading
            the correct index views if `index_view_key` is not set.
        dest_path: Colored mesh will be written to k.zip and not returned.
        k: Number of nearest vertices to average over. If k=0 unpredicted vertices
            will be treated as 'unpredicted' class.
        colors: Array with as many entries as the maximum label of 'semseg_key'
            predictions with values between 0 and 255 (will be interpreted as uint8).
            If it is None, the majority label according to kNN will be returned
            instead. Note to add a color for unpredicted vertices if k==0; here
            illustrated with by the spine prediction example:
            if k=0: [neck, head, shaft, other, background, unpredicted]
            else: [neck, head, shaft, other, background].
        force_recompute: Force re-mapping of the predicted labels to the
            mesh vertices.

    Returns:
        indices, vertices, normals, color
    """
    ld = sso.label_dict('vertex')
    if force_recompute or semseg_key not in ld:
        ts0 = time.time()  # view loading
        if nb_views is None and index_view_key is None:
            # load default
            i_views = sso.load_views(index_views=True).flatten()
        else:
            if index_view_key is None:
                index_view_key = "index{}".format(nb_views)
            # load special views
            i_views = sso.load_views(index_view_key).flatten()
        semseg_views = sso.load_views(semseg_key).flatten()
        ts1 = time.time()
        # log_reps.debug('Time to load index and shape views: '
        #                '{:.2f}s.'.format(ts1 - ts0))
        background_id = np.max(i_views)
        background_l = np.max(semseg_views)
        unpredicted_l = background_l + 1
        pp = len(sso.mesh[1]) // 3
        count_arr = np.zeros((pp, background_l + 1), dtype=np.uint8)
        count_arr = semseg2mesh_counter(i_views, semseg_views, background_id,
                                        count_arr)
        # np.argmax returns int64 array.. `colorcode_vertices` complexity is
        # sensitive to the datatype of vertex_labels!
        vertex_labels = np.argmax(count_arr, axis=1).astype(np.uint8)
        mask = np.sum(count_arr, axis=1) == 0
        vertex_labels[mask] = unpredicted_l
        # background label is highest label in prediction (see 'generate_palette' or
        # 'remap_rgb_labelviews' in multiviews.py)
        if unpredicted_l > 255:
            raise ValueError('Overflow in label view array.')
        if k == 0:  # map actual prediction situation / coverage
            # keep unpredicted vertices and vertices with background labels
            predicted_vertices = sso.mesh[1].reshape(-1, 3)
            predictions = vertex_labels
        else:
            # remove unpredicted vertices
            predicted_vertices = sso.mesh[1].reshape(-1, 3)[vertex_labels != unpredicted_l]
            predictions = vertex_labels[vertex_labels != unpredicted_l]
            # remove background class
            predicted_vertices = predicted_vertices[predictions != background_id]
            predictions = predictions[predictions != background_id]
        ts2 = time.time()
        # log_reps.debug('Time to map predictions on vertices: '
        #                '{:.2f}s.'.format(ts2 - ts1))
        # High time complexity!
        if k > 0:  # map predictions of predicted vertices to all vertices
            maj_vote = colorcode_vertices(
                sso.mesh[1].reshape((-1, 3)), predicted_vertices, predictions,
                k=k,
                return_color=False, nb_cpus=sso.nb_cpus)
            ts3 = time.time()
            # log_reps.debug('Time to map predictions on unpredicted vertices'
            #                'with k={}: {:.2f}s.'.format(k, ts3 - ts2))
        else:  # no vertex mask was applied in this case
            maj_vote = predictions

        # add prediction to mesh storage
        ld[semseg_key] = maj_vote
        ld.push()
    else:
        maj_vote = ld[semseg_key]
    if colors is not None:
        col = colors[maj_vote].astype(np.uint8)
        if np.sum(col) == 0:
            log_reps.warn('All colors-zero warning during "semseg2mesh"'
                          ' of SSO {}. Make sure color values have uint8 range '
                          '0...255'.format(sso.id))
    else:
        col = maj_vote
    if dest_path is not None:
        if colors is None:
            col = None  # set to None, because write_mesh2kzip only supports
            # RGBA colors and no labels
        write_mesh2kzip(dest_path, sso.mesh[0], sso.mesh[1], sso.mesh[2],
                        col, ply_fname=semseg_key + ".ply")
        return
    return sso.mesh[0], sso.mesh[1], sso.mesh[2], col


def celltype_of_sso_nocache(sso, model, ws, nb_views, comp_window, nb_views_model=20,
                            pred_key_appendix="", verbose=False,
                            overwrite=True, use_syntype=True,
                            da_equals_tan=True):
    """
    Renders raw views at rendering locations determined by `comp_window`
    and according to given view properties without storing them on the file
    system. Views will be predicted with the given `model`. By default,
    resulting predictions and probabilities are stored as 'celltype_cnn_e3'
    and 'celltype_cnn_e3_probas' in the attribute dictionary.

    Args:
        sso:
        model:
        ws: Window size in pixels [y, x]
        nb_views: Number of views rendered at each rendering location.
        nb_views_model: bootstrap sample size of view locations for model prediction
        comp_window: Physical extent in nm of the view-window along y (see `ws` to infer pixel size)
        pred_key_appendix:
        verbose: Adds progress bars for view generation.
        overwrite:
        use_syntype: Use type of presynaptic synapses.
        da_equals_tan:

    Returns:

    """
    sso.load_attr_dict()
    pred_key = "celltype_cnn_e3" + pred_key_appendix
    if not overwrite and pred_key in sso.attr_dict:
        return

    view_kwargs = dict(ws=ws, comp_window=comp_window, nb_views=nb_views, verbose=verbose, add_cellobjects=True,
                       return_rot_mat=False)
    verts = sso.mesh[1].reshape(-1, 3)
    rendering_locs = generate_rendering_locs(verts, comp_window / 3)  # three views per comp window

    # overwrite default rendering locations (used later on for the view generation)
    sso._sample_locations = rendering_locs
    # this cache is only in-memory, and not file system cache
    assert sso.view_caching, "'view_caching' of {} has to be True in order to" \
                             " run 'celltype_of_sso_nocache'.".format(sso)

    tmp_view_key = 'tmp_views' + pred_key_appendix
    if tmp_view_key not in sso.view_dict or overwrite:
        views = render_sso_coords(sso, rendering_locs, **view_kwargs)  # shape: N, 4, nb_views, y, x
        sso.view_dict[tmp_view_key] = views  # required for `sso_views_to_modelinput`

    from ..handler.prediction import naive_view_normalization_new
    if verbose:
        log_reps.debug('Finished rendering. Starting cell type prediction.')
    inp_d = sso_views_to_modelinput(sso, nb_views_model, view_key=tmp_view_key)
    inp_d = naive_view_normalization_new(inp_d)
    if use_syntype:
        synsign_ratio = np.array([[syn_sign_ratio_celltype(sso, comp_types=[1, ]),
                                   syn_sign_ratio_celltype(sso, comp_types=[0, ])]] * len(inp_d))
        res = model.predict_proba((inp_d, synsign_ratio), bs=40)
    else:
        res = model.predict_proba(inp_d, bs=40)
    if verbose:
        log_reps.debug('Finished prediction.')
    # DA and TAN are type modulatory, if this is changes, also change `certainty_celltype`, `predict_sso_celltype`
    if da_equals_tan:
        # accumulate evidence for DA and TAN
        res[:, 1] += res[:, 6]
        # remove TAN in proba array
        res = np.delete(res, [6], axis=1)
        # INT is now at index 6 -> label 6 is INT

    clf = np.argmax(res, axis=1)
    if np.max(clf) >= 7:
        raise ValueError('Unknown cell type predicted.')
    major_dec = np.zeros(7)
    for ii in range(len(major_dec)):
        major_dec[ii] = np.sum(clf == ii)
    major_dec /= np.sum(major_dec)
    pred = np.argmax(major_dec)
    sso.attr_dict[pred_key] = pred
    sso.attr_dict[f"{pred_key}_probas"] = res
    sso.save_attributes([pred_key], [pred])
    sso.save_attributes([f"{pred_key}_probas"], [res])


def view_embedding_of_sso_nocache(sso: 'SuperSegmentationObject', model: 'torch.nn.Module', ws: Tuple[int, int],
                                  nb_views: int, comp_window: int, pred_key_appendix: str = "",
                                  verbose: bool = False, overwrite: bool = True, dest_path: Optional[str] = None,
                                  add_cellobjects: Union[bool, Iterable] = True):
    """
    Renders raw views at rendering locations determined by `comp_window`
    and according to given view properties without storing them on the file system. Views will
    be predicted with the given `model`. See `predict_views_embedding` in `super_segmentation_object`
    for an alternative which uses file-system cached views.
    By default, resulting predictions are stored as `latent_morph`.

    Args:
        sso:
        model:
        ws: Window size in pixels [y, x]
        nb_views: Number of views rendered at each rendering location.
        comp_window: Physical extent in nm of the view-window along y (see `ws`
            to infer pixel size).
        pred_key_appendix:
        verbose: Adds progress bars for view generation.
        overwrite: Overwrite existing views in temporary view dictionary. Key: ``'tmp_views' + pred_key_appendix``.
        dest_path: Destination path of k.zip file.
        add_cellobjects: Add cell objects. Either bool or list of structures used to render. Only
            used when `raw_view_key` or `nb_views` is None - then views are rendered on-the-fly.

    """
    pred_key = "latent_morph"
    pred_key += pred_key_appendix
    view_kwargs = dict(ws=ws, comp_window=comp_window, nb_views=nb_views,
                       verbose=verbose, add_cellobjects=add_cellobjects,
                       return_rot_mat=False)
    verts = sso.mesh[1].reshape(-1, 3)
    # this cache is only in-memory, and not file system cache
    assert sso.view_caching, "'view_caching' of {} has to be True in order to" \
                             " run 'view_embedding_of_sso_nocache'.".format(sso)
    tmp_view_key = 'tmp_views' + pred_key_appendix
    if tmp_view_key not in sso.view_dict or overwrite:
        rendering_locs = generate_rendering_locs(verts, comp_window / 3)  # ~3 views per comp window

        # overwrite default rendering locations (used later on for the view generation)
        sso._sample_locations = rendering_locs[None, ]  # requires auxiliary axis
        # views shape: N, 4, nb_views, y, x
        views = render_sso_coords(sso, rendering_locs, **view_kwargs)
        sso.view_dict[tmp_view_key] = views  # required for `sso_views_to_modelinput`
    else:
        views = sso.view_dict[tmp_view_key]
    from ..handler.prediction import naive_view_normalization_new

    views = naive_view_normalization_new(views)
    # The inference with TNets can be optimzed, via splititng the views into three equally sized parts.
    inp = (views[:, :, 0], np.zeros_like(views[:, :, 0]), np.zeros_like(views[:, :, 0]))
    # return dist1, dist2, inp1, inp2, inp3 latent
    _, _, latent, _, _ = model.predict_proba(inp)  # only use first view for now

    # map latent vecs at rendering locs to skeleton node locations via nearest neighbor
    sso.load_skeleton()
    # view location ordering same as views / latent
    hull_tree = spatial.cKDTree(np.concatenate(sso.sample_locations()))
    dists, ixs = hull_tree.query(sso.skeleton["nodes"] * sso.scaling, n_jobs=sso.nb_cpus, k=1)
    sso.skeleton[pred_key] = latent[ixs]
    if dest_path is not None:
        return sso.save_skeleton_to_kzip(dest_path=dest_path)
    sso.save_skeleton()


def semseg_of_sso_nocache(sso, model, semseg_key: str, ws: Tuple[int, int],
                          nb_views: int, comp_window: float, k: int = 1,
                          dest_path: Optional[str] = None, verbose: bool = False,
                          add_cellobjects: Union[bool, Iterable] = True):
    """
    Renders raw and index views at rendering locations determined by `comp_window`
    and according to given view properties without storing them on the file system. Views will
    be predicted with the given `model` and maps prediction results onto mesh.
    Vertex labels are stored on file system and can be accessed via
    `sso.label_dict('vertex')[semseg_key]`.
    If sso._sample_locations is None it `generate_rendering_locs(verts, comp_window / 3)`
    will be called to generate rendering locations.

    Examples:
        Given a cell reconstruction exported as kzip (see ) at ``cell_kzip_fn``
        the compartment prediction (axon boutons, dendrite, soma) can be started
        via the following script::

            # set working directory to obtain models
            global_params.wd = '~/SyConn/example_cube1/'

            # get model for compartment detection
            m = get_semseg_axon_model()
            view_props = global_params.config['compartments']['view_properties_semsegax']
            view_props["verbose"] = True

            # load SSO instance from k.zip file
            sso = init_sso_from_kzip(cell_kzip_fn, sso_id=1)

            # run prediction and store result in new kzip
            cell_kzip_fn_axon = cell_kzip_fn[:-6] + '_axon.k.zip'
            semseg_of_sso_nocache(sso, dest_path=cell_kzip_fn_axon, model=m,
                                  **view_props)

        See also the example scripts at::

            $ python SyConn/examples/semseg_axon.py
            $ python SyConn/examples/semseg_spine.py

    Args:
        sso: Cell reconstruction object.
        model: The elektronn3 model used for the prediction.
        semseg_key: The key which is used to store the resulting prediction.
        ws: Window size in pixels [y, x]
        nb_views: Number of views rendered at each rendering location.
        comp_window: Physical extent in nm of the view-window along y (see `ws` to infer pixel size)
        k: Number of nearest vertices to average over. If k=0 unpredicted vertices will
            be treated as 'unpredicted' class.
        dest_path: location of kzip in which colored vertices (according to semantic
            segmentation prediction) are stored.
        verbose: Adds progress bars for view generation.
        add_cellobjects: Add cell objects. Either bool or list of structures used to render. Only
            used when `raw_view_key` or `nb_views` is None - then views are rendered on-the-fly.

    Returns:

    """
    view_kwargs = dict(ws=ws, comp_window=comp_window, nb_views=nb_views,
                       verbose=verbose, save=False)
    raw_view_key = 'raw{}_{}_{}'.format(ws[0], ws[1], nb_views)
    index_view_key = 'index{}_{}_{}'.format(ws[0], ws[1], nb_views)
    verts = sso.mesh[1].reshape(-1, 3)

    # use default rendering locations (used later on for the view generation)
    if sso._sample_locations is None:
        # ~three views per comp window
        rendering_locs = generate_rendering_locs(verts, comp_window / 3)
        sso._sample_locations = [rendering_locs]
    assert sso.view_caching, "'view_caching' of {} has to be True in order to" \
                             " run 'semseg_of_sso_nocache'.".format(sso)
    # this generates the raw views and their prediction
    sso.predict_semseg(model, semseg_key, raw_view_key=raw_view_key,
                       add_cellobjects=add_cellobjects, **view_kwargs)
    if verbose:
        log_reps.debug('Finished shape-view rendering and sem. seg. prediction.')
    # this generates the index views
    sso.render_indexviews(view_key=index_view_key, force_recompute=True,
                          **view_kwargs)
    if verbose:
        log_reps.debug('Finished index-view rendering.')
    # map prediction onto mesh and saves it to sso._label_dict['vertex'][semseg_key] (also pushed to file system!)
    sso.semseg2mesh(semseg_key, index_view_key=index_view_key, dest_path=dest_path,
                    force_recompute=True, k=k)
    if verbose:
        log_reps.debug('Finished mapping of vertex predictions to mesh.')


# TODO: figure out how to enable type hinting without explicitly importing the classes.
def assemble_from_mergelist(ssd, mergelist: Union[Dict[int, int], str]):
    """
    Creates,
    :attr:`~syconn.reps.super_segmentation_dataset.SuperSegmentationDataset.mapping_dict` and
    :attr:`~syconn.reps.super_segmentation_dataset.SuperSegmentationDataset.id_changer` and finally calls
    :func:`~syconn.reps.super_segmentation_dataset.SuperSegmentationDataset.save_dataset_shallow`.

    Args:
        ssd: SuperSegmentationDataset
        mergelist: Definition of supervoxel agglomeration.

    """
    if mergelist is not None:
        assert "sv" in ssd.version_dict
        if isinstance(mergelist, dict):
            pass
        elif isinstance(mergelist, str):
            with open(mergelist, "r") as f:
                mergelist = mergelist_tools. \
                    subobject_map_from_mergelist(f.read())
        else:
            raise Exception("sv_mapping has unknown type")


    for sv_id in mergelist.values():
        ssd.mapping_dict[sv_id] = []

    # Changed -1 defaults to 0
    # ssd._id_changer = np.zeros(np.max(list(mergelist.keys())) + 1,
    #                           dtype=np.uint)
    # TODO: check if np.int might be a problem for big datasets
    ssd._id_changer = np.ones(int(np.max(list(mergelist.keys())) + 1),
                              dtype=np.int) * (-1)

    for sv_id in mergelist.keys():
        ssd.mapping_dict[mergelist[sv_id]].append(sv_id)
        ssd._id_changer[sv_id] = mergelist[sv_id]

    ssd.save_dataset_shallow()


def compartments_graph(ssv: 'super_segmentation.SuperSegmentationObject',
                       axoness_key: str) -> Tuple[nx.Graph, nx.Graph, nx.Graph]:
    """
    Creates a axon, dendrite and soma graph based on the skeleton node
    CMN predictions.

    Args:
        ssv: Cell reconstruction. Its skeleton must exist and must contain keys
        ``'edges'``, ``'nodes'`` and `axoness_key`.
        axoness_key: Key used to retrieve axon predictions in ``ssv.skeleton``
            (0: dendrite, 1: axon, 2: soma). Converts labels 3 (en-passant bouton)
            and 4 (terminal bouton) into 1 (axon).

    Returns:
        Three graphs for dendrite, axon and soma compartment respectively.
    """
    axon_prediction = np.array(ssv.skeleton[axoness_key])
    axon_prediction[axon_prediction == 3] = 1
    axon_prediction[axon_prediction == 4] = 1
    axon_ixs = np.nonzero(axon_prediction == 1)
    dendrite_ixs = np.nonzero(axon_prediction == 0)
    soma_ixs = np.nonzero(axon_prediction == 2)
    so_graph = ssv.weighted_graph(add_node_attr=[axoness_key])
    ax_graph = so_graph.copy()
    den_graph = so_graph.copy()
    for ix in axon_ixs:
        so_graph.remove_node(ix)
        den_graph.remove_node(ix)
    for ix in dendrite_ixs:
        so_graph.remove_node(ix)
        ax_graph.remove_node(ix)
    for ix in soma_ixs:
        ax_graph.remove_node(ix)
        den_graph.remove_node(ix)
    return den_graph, ax_graph, so_graph


def syn_sign_ratio_celltype(ssv: 'super_segmentation.SuperSegmentationObject', weighted: bool = True,
                            recompute: bool = True, comp_types: Optional[List[int]] = None) -> float:
    """
    Ratio of symmetric synapses (between 0 and 1; -1 if no synapse objects)
    on specified functional compartments (`comp_types`) of the cell
    reconstruction. Does not include compartment information of the partner
    cell. See :func:`~syconn.reps.super_segmentation_object.SuperSegmentationObject.syn_sign_ratio`
     for this.

    Todo:
        * Check default of synapse type if synapse type predictions are not
          available -> propagate to this method and return -1.

    Notes:
        * Bouton predictions are converted into axon label, i.e. 3 -> 1 (en-passant) and 4 -> 1 (terminal).

        * The compartment predictions are collected after the first access of this attribute
          during the celltype prediction. The key 'partner_axoness' is not available within ``
          self.syn_ssv`` until :func:`~syconn.extraction.cs_processing_steps
          ._collect_properties_from_ssv_partners_thread` is called (see
          :func:`~syconn.exec.exec_syns.run_matrix_export`).

        * The compartment type of the other cell cannot be inferred at this
          point. Think about adding the property collection before celltype
          prediction -> would allow more detailed filtering of the synapses,
          but adds an additional round of property collection.

    Args:
        ssv: The cell reconstruction.
        weighted: Compute synapse-area weighted ratio.
        recompute: Ignore existing value.
        comp_types: All synapses that are formed between any of the functional compartment types given in
            `comp_types` on the cell reconstruction are used for computing the ratio (0: dendrite, 1: axon, 2:
             soma). Default: [1, ].

    Returns:
        (Area-weighted) ratio of symmetric synapses or -1 if no synapses.
    """
    if comp_types is None:
        comp_types = [1, ]
    ratio = ssv.lookup_in_attribute_dict("syn_sign_ratio")
    if not recompute and ratio is not None:
        return ratio
    pred_key_ax = "{}_avg{}".format(global_params.config['compartments']['view_properties_semsegax']['semseg_key'],
                                    global_params.config['compartments']['dist_axoness_averaging'])
    if len(ssv.syn_ssv) == 0:
        return -1
    props = load_so_attr_bulk(ssv.syn_ssv, ('syn_sign', 'mesh_area', 'rep_coord'), allow_missing=True,
                              use_new_subfold=global_params.config.use_new_subfold)
    syn_axs = ssv.attr_for_coords([props['rep_coord'][syn.id] for syn in ssv.syn_ssv], attr_keys=[pred_key_ax, ])[0]
    # convert boutons to axon class
    syn_axs[syn_axs == 3] = 1
    syn_axs[syn_axs == 4] = 1
    syn_signs = []
    syn_sizes = []
    for syn_ix, syn in enumerate(ssv.syn_ssv):
        if syn_axs[syn_ix] not in comp_types:
            continue
        syn_sign = props['syn_sign'][syn.id]
        syn_size = props['mesh_area'][syn.id] / 2
        if syn_sign is None or syn_size is None:
            raise ValueError(f'Got at least one None value for syn_sign and/or syn_size of {ssv.syn_ssv[syn_ix]}.')
        syn_signs.append(syn_sign)
        syn_sizes.append(syn_size)
    if len(syn_signs) == 0 or np.sum(syn_sizes) == 0:
        return -1
    syn_signs = np.array(syn_signs)
    syn_sizes = np.array(syn_sizes)
    if weighted:
        ratio = np.sum(syn_sizes[syn_signs == -1]) / float(np.sum(syn_sizes))
    else:
        ratio = np.sum(syn_signs == -1) / float(len(syn_signs))
    return ratio


def extract_spinehead_volume_mesh(sso: 'super_segmentation.SuperSegmentationObject',
                                  ctx_vol=(200, 200, 100)):
    """
    Calculate the volume of spine heads based on a watershed procedure on the
    cell segmentation. Connected components of spine head skeleton nodes
    are used as starting point to collect mesh vertices with spine predictions
    within at least ``2*ctx_vol``. The watershed seeds are extracted from local maxima of the
    cell mask's distance transform. Each seed is assigned the majority label of its
    k-nearest vertices.
    Results are stored in :attr:`~syconn.reps.super_segmentation_object.SuperSegmentationObject
    .skeleton` with the key ``spinehead_vol``.

    Notes:
        * Requires a (loaded, ``sso.load_skeleton``) skeleton, i.e. ``sso.skeleton`` must be present.
        * If the results have to be stored, call ``sso.save_skeleton()``

    Args:
        sso: Cell object.
        ctx_vol: Additional volume above and below the bounding box of the extracted
            connected component spine head skeleton nodes, i.e. the inspected volume is
            at least ``2*ctx_vol``.
    """
    # use bigger skel context to get the correspondence to the voxel as accurate as possible
    ctx_vol = np.array(ctx_vol)
    scaling = sso.scaling
    if 'spiness' not in sso.skeleton:
        log_reps.warn(f'"spiness" not available in skeleton of SSO {sso.id}. '
                      f'Skipping.')
        sso.skeleton['spinehead_vol'] = np.zeros((len(sso.skeleton['nodes']),)).astype(np.float32)
        return
    ssv_svids = set(sso.sv_ids)
    sso.skeleton['spinehead_vol'] = np.zeros_like(sso.skeleton['spiness']).astype(np.float32)
    ssv_syncoords = np.array([syn.rep_coord for syn in sso.syn_ssv])
    if len(ssv_syncoords) == 0:
        return
    ssv_synids = np.array([syn.id for syn in sso.syn_ssv])
    verts = sso.mesh[1].reshape(-1, 3) / scaling
    sp_semseg = sso.label_dict('vertex')['spiness']
    ignore_labels = global_params.config['spines']['semseg2coords_spines']['ignore_labels']
    for l in ignore_labels:
        verts = verts[sp_semseg != l]
        sp_semseg = sp_semseg[sp_semseg != l]
    curr_sp = sso.semseg_for_coords(ssv_syncoords, 'spiness',
                                    **global_params.config['spines']['semseg2coords_spines'])
    pred_key_ax = "{}_avg{}".format(global_params.config['compartments'][
                                        'view_properties_semsegax']['semseg_key'],
                                    global_params.config['compartments'][
                                        'dist_axoness_averaging'])
    curr_ax = sso.attr_for_coords(ssv_syncoords, attr_keys=[pred_key_ax])[0]
    ssv_syncoords = ssv_syncoords[(curr_sp == 1) & (curr_ax == 0)]
    ssv_synids = ssv_synids[(curr_sp == 1) & (curr_ax == 0)]
    if len(ssv_syncoords) == 0:  # node spine head synapses
        return
    kdt = spatial.KDTree(sso.skeleton["nodes"] * sso.scaling)
    _, close_node_ids = kdt.query(ssv_syncoords * sso.scaling, k=1)
    # iterate over connected skeleton nodes labeled as spine head
    for c, node_ix, ssv_id in zip(ssv_syncoords, close_node_ids, ssv_synids):
        # get closest skeleton node
        bb = np.array([np.min([c], axis=0), np.max([c], axis=0)])
        offset = bb[0] - ctx_vol
        size = (bb[1] - bb[0] + 1 + 2 * ctx_vol).astype(np.int)
        # get cell segmentation mask
        kd = kd_factory(global_params.config.kd_seg_path)
        seg = kd.load_seg(offset=offset, size=size, mag=1).swapaxes(2, 0)
        orig_sh = seg.shape
        seg = seg.flatten()
        for ii, el in enumerate(seg):
            if el not in ssv_svids:
                seg[ii] = 0
            else:
                seg[ii] = 1
        seg = seg.reshape(orig_sh)
        seg = ndimage.binary_fill_holes(seg)
        # set watershed seeds using vertices
        vert_ixs_bb = in_bounding_box(verts, np.array([offset + size / 2, size]))
        vert_ixs_bb = np.array(vert_ixs_bb, dtype=np.bool)
        verts_bb = verts[vert_ixs_bb]
        semseg_bb = sp_semseg[vert_ixs_bb]
        # relabelled spine neck as 9, actually not needed here
        semseg_bb[semseg_bb == 0] = 9

        distance = ndimage.distance_transform_edt(seg)
        local_maxi = peak_local_max(distance, indices=False, footprint=np.ones((3, 3, 3)),
                                    labels=seg).astype(np.uint)
        maxima = np.transpose(np.nonzero(local_maxi))
        # assign labels from nearby vertices
        maxima_sp = colorcode_vertices(maxima, verts_bb - offset, semseg_bb,
                                       k=global_params.config['spines']['semseg2coords_spines']['k'],
                                       return_color=False, nb_cpus=sso.nb_cpus)
        local_maxi[maxima[:, 0], maxima[:, 1], maxima[:, 2]] = maxima_sp

        labels = watershed(-distance, local_maxi, mask=seg).astype(np.uint64)
        labels[labels != 1] = 0  # only keep spine head locations
        labels, nb_obj = ndimage.label(labels)
        c = c - offset
        max_id = 1
        if nb_obj > 1:
            # query many voxels or use NN approach?
            ls = labels[(c[0] - 20):(c[0] + 21), (c[1] - 20):(c[1] + 21),
                 (c[2] - 10):(c[2] + 11)]
            ids, cnts = np.unique(ls, return_counts=True)
            cnts = cnts[ids != 0]
            ids = ids[ids != 0]
            if len(ids) == 0:
                coords = []
                ids = []
                for ii in range(1, nb_obj + 1):
                    curr_coords = np.transpose(np.nonzero(labels == ii))
                    coords.append(curr_coords)
                    ids.extend(len(curr_coords) * [ii])
                coords = np.concatenate(coords) + offset
                nn_kdt = spatial.cKDTree(coords * sso.scaling)
                _, nn_id = nn_kdt.query([(c + offset) * sso.scaling])
                max_id = ids[nn_id[0]]
            else:
                max_id = ids[np.argmax(cnts)]

        n_voxels_spinehead = np.sum(labels == max_id)
        vol_sh = n_voxels_spinehead * np.prod(scaling) / 1e9  # in um^3
        sso.skeleton['spinehead_vol'][node_ix] = vol_sh


def sso_svgraph2kzip(dest_path: str, sso: 'SuperSegmentationObject'):
    """
    Store SV graph in KNOSSOS compatible kzip.

    Args:
        dest_path: Path to k.zip.
        sso: Cell object.
    """
    sv_edges = sso.load_sv_edgelist()
    anno = SkeletonAnnotation()
    anno.scaling = sso.scaling
    sd = sso.get_seg_dataset('sv')
    coord_dc = dict()
    for ii in range(len(sd.ids)):
        coord_dc[sd.ids[ii]] = sd.rep_coords[ii]
    for sv1, sv2 in sv_edges:
        sv1_coord = coord_dc[sv1.id] * sso.scaling
        sv2_coord = coord_dc[sv2.id] * sso.scaling
        n1 = SkeletonNode().from_scratch(anno, sv1_coord[0], sv1_coord[1], sv1_coord[2])
        n1.data['svid'] = sv1.id
        n2 = SkeletonNode().from_scratch(anno, sv2_coord[0], sv2_coord[1], sv2_coord[2])
        n2.data['svid'] = sv2.id
        anno.addNode(n1)
        anno.addNode(n2)
        anno.addEdge(n1, n2)
    dummy_skel = Skeleton()
    dummy_skel.add_annotation(anno)
    dummy_skel.to_kzip(dest_path)
