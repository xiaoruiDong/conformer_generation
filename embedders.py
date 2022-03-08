#!/usr/bin/env python3
#-*- coding: utf-8 -*-

"""
Modules for providing initial guess geometries
"""

from rdmc import RDKitMol
import os.path as osp
import yaml
import numpy as np
from time import time
import torch
try:
    from torch_geometric.data import Batch
except ImportError:
    pass
from .utils import *

try:
    from rdmc.external.GeoMol.model.model import GeoMol
    from rdmc.external.GeoMol.model.featurization import featurize_mol_from_smiles
    from rdmc.external.GeoMol.model.inference import construct_conformers
except ImportError:
    print("No GeoMol installation detected. Skipping import...")


class ConfGenEmbedder:
    def __init__(self, track_stats=False):

        self.iter = 0
        self.track_stats = track_stats
        self.n_success = None
        self.percent_success = None
        self.stats = []
        self.smiles = None

    def update_mol(self, smiles):
        # Only update the molecule if smiles is changed
        # Only copy the molecule graph from the previous run rather than conformers
        if smiles != self.smiles:
            self.smiles = smiles
            self.mol = RDKitMol.FromSmiles(smiles)
        else:
            # Copy the graph but remove conformers
            self.mol = self.mol.Copy(quickCopy=True)

    def embed_conformers(self, n_conformers):
        raise NotImplementedError

    def update_stats(self,
                     n_trials,
                     time=0.):
        n_success = self.mol.GetNumConformers()
        self.n_success = n_success
        self.percent_success = n_success / n_trials * 100
        stats = {"iter": self.iter,
                 "time": time,
                 "n_success": self.n_success,
                 "percent_success": self.percent_success}
        self.stats.append(stats)
        return stats

    def write_mol_data(self):
        return mol_to_dict(self.mol, copy=False, iter=self.iter)

    def __call__(self, smiles, n_conformers):

        self.iter += 1
        time_start = time()
        self.update_mol(smiles)
        self.embed_conformers(n_conformers)
        mol_data = self.write_mol_data()

        if not self.track_stats:
            return mol_data

        time_end = time()
        self.update_stats(n_trials=n_conformers,
                          time=time_end - time_start)
        return mol_data


class GeoMolEmbedder(ConfGenEmbedder):
    def __init__(self, trained_model_dir, dataset="drugs", temp_schedule="linear", track_stats=False):
        super(GeoMolEmbedder, self).__init__(track_stats)

        # TODO: add option of pre-pruning geometries using alpha values
        # TODO: inverstigate option of changing "temperature" each iteration to sample diverse geometries

        with open(osp.join(trained_model_dir, "model_parameters.yml")) as f:
            model_parameters = yaml.full_load(f)
        model = GeoMol(**model_parameters)

        state_dict = torch.load(osp.join(trained_model_dir, "best_model.pt"), map_location=torch.device('cpu'))
        model.load_state_dict(state_dict, strict=True)
        model.eval()
        self.model = model
        self.tg_data = None
        self.std = model_parameters["hyperparams"]["random_vec_std"]
        self.temp_schedule = temp_schedule
        self.dataset = dataset

    def embed_conformers(self, n_conformers):

        # set "temperature"
        if self.temp_schedule == "none":
            self.model.random_vec_std = self.std
        elif self.temp_schedule == "linear":
            self.model.random_vec_std = self.std * (1 + self.iter / 10)

        # featurize data and run GeoMol
        if self.tg_data is None:
            self.tg_data = featurize_mol_from_smiles(self.smiles, dataset=self.dataset)
        data = Batch.from_data_list([self.tg_data])  # need to run this bc of dumb internal GeoMol processing
        self.model(data, inference=True, n_model_confs=n_conformers)

        # process predictions
        model_coords = construct_conformers(data, self.model).double().cpu().detach().numpy()
        split_model_coords = np.split(model_coords, n_conformers, axis=1)

        # package in mol and return
        self.mol.EmbedMultipleNullConfs(n=n_conformers, random=False)
        for i, x in enumerate(split_model_coords):
            conf = self.mol.GetConformer(i)
            conf.SetPositions(x.squeeze(axis=1))
        return self.mol

class ETKDGEmbedder(ConfGenEmbedder):

    def embed_conformers(self, n_conformers):
        self.mol.EmbedMultipleConfs(n_conformers)
        return self.mol

class RandomEmbedder(ConfGenEmbedder):

    def embed_conformers(self, n_conformers):
        self.mol.EmbedMultipleNullConfs(n_conformers, random=True)
        return self.mol
