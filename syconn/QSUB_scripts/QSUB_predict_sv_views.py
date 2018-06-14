import os

import sys
import numpy as np
try:
    import cPickle as pkl
# TODO: switch to Python3 at some point and remove above
except Exception:
    import pickle as pkl
from syconn.reps.super_segmentation import render_sampled_sos_cc
from syconn.proc.sd_proc import sos_dict_fact, init_sos, predict_sos_views
from syconn.handler.prediction import NeuralNetworkInterface
path_storage_file = sys.argv[1]
path_out_file = sys.argv[2]

with open(path_storage_file) as f:
    args = []
    while True:
        try:
            args.append(pkl.load(f))
        except:
            break

svixs = args[0]
model_kwargs = args[1]
so_kwargs = args[2]
pred_kwargs = args[3]

model = NeuralNetworkInterface(**model_kwargs)
sd = sos_dict_fact(svixs, **so_kwargs)
sos = init_sos(sd)
out = predict_sos_views(model, sos, **pred_kwargs)

with open(path_out_file, "wb") as f:
    pkl.dump(out, f)