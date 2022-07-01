from typing import List, Union, Tuple

import sys
import os

import numpy as np
from rdkit import Chem
import torch
import torch.nn as nn

from .mpn import MPN
from chemprop.args import TrainArgs
from chemprop.features import BatchMolGraph
from chemprop.nn_utils import get_activation_function, initialize_weights


class MoleculeModel(nn.Module):
    """A :class:`MoleculeModel` is a model which contains a message passing network following by feed-forward layers."""

    def __init__(self, args: TrainArgs):
        """
        :param args: A :class:`~chemprop.args.TrainArgs` object containing model arguments.
        """
        super(MoleculeModel, self).__init__()

        self.classification = args.dataset_type == 'classification'
        self.multiclass = args.dataset_type == 'multiclass'
        if args.custom_func_dir is not None:
            try:
                del sys.modules['custom_func']
                sys.path.insert(1, os.path.abspath(args.custom_func_dir))
                import custom_func
            except:
                sys.path.insert(1, os.path.abspath(args.custom_func_dir))
                import custom_func
            del sys.path[1]
            self.custom_func = custom_func.custom_func
            self.modify_features = custom_func.modify_features
            self.extra_outputs = custom_func.extra_outputs
            self.extra_features = custom_func.extra_features
        
        else:
            self.custom_func = None
            self.extra_outputs = 0
            self.extra_features = 0
        self.output_size = args.num_tasks
        if self.multiclass:
            self.output_size *= args.multiclass_num_classes

        if self.classification:
            self.sigmoid = nn.Sigmoid()

        if self.multiclass:
            self.multiclass_softmax = nn.Softmax(dim=2)

        self.create_encoder(args)
        
        if args.multi_branch_ffn is not None:
            self.create_multi_branch_ffn(args)
            self.is_multi_branch = True
        elif type(args.ffn_hidden_size) in (tuple, list):
            self.create_ffn_from_tuple(args)
            self.is_multi_branch = False
        else:
            self.create_ffn(args)
            self.is_multi_branch = False

        initialize_weights(self)
        

    def create_encoder(self, args: TrainArgs) -> None:
        """
        Creates the message passing encoder for the model.

        :param args: A :class:`~chemprop.args.TrainArgs` object containing model arguments.
        """
        self.encoder = MPN(args)

        if args.checkpoint_frzn is not None:
            if args.freeze_first_only: # Freeze only the first encoder
                for param in list(self.encoder.encoder.children())[0].parameters():
                    param.requires_grad=False
            else: # Freeze all encoders
                for param in self.encoder.parameters():
                    param.requires_grad=False

        
    def create_ffn_from_tuple(self, args: TrainArgs) -> None:
        """
        Creates the feed-forward layers for the model.

        :param args: A :class:`~chemprop.args.TrainArgs` object containing model arguments.
        """
        self.multiclass = args.dataset_type == 'multiclass'
        if self.multiclass:
            self.num_classes = args.multiclass_num_classes
        if args.features_only:
            first_linear_dim = (args.features_size + self.extra_features)
        else:
            first_linear_dim = args.hidden_size * args.number_of_molecules
            if args.use_input_features:
                first_linear_dim += (args.features_size + self.extra_features)

        if args.atom_descriptors == 'descriptor':
            first_linear_dim += args.atom_descriptors_size

        dropout = nn.Dropout(args.dropout)
        activation = get_activation_function(args.activation)

        # Create FFN layers
        if args.ffn_num_layers == 1:
            ffn = [
                dropout,
                nn.Linear(first_linear_dim, self.output_size
                                      + self.extra_outputs)
            ]
        else:
            ffn = [
                dropout,
                nn.Linear(first_linear_dim, args.ffn_hidden_size[0])
            ]
            for i in range(args.ffn_num_layers - 2):
                ffn.extend([
                    activation,
                    dropout,
                    nn.Linear(args.ffn_hidden_size[i], args.ffn_hidden_size[i+1]),
                ])
            ffn.extend([
                activation,
                dropout,
                nn.Linear(args.ffn_hidden_size[-1], self.output_size
                                        + self.extra_outputs),
            ])

        # Create FFN model
        self.ffn = nn.Sequential(*ffn)
        
    def create_multi_branch_ffn(self, args: TrainArgs) -> None:
        """
        Creates the feed-forward layers for the model.

        :param args: A :class:`~chemprop.args.TrainArgs` object containing model arguments.
        """
        
        if args.features_only:
            first_linear_dim = (args.features_size + self.extra_features)
        else:
            first_linear_dim = args.hidden_size * args.number_of_molecules
            if args.use_input_features:
                first_linear_dim += (args.features_size + self.extra_features)

        if args.atom_descriptors == 'descriptor':
            first_linear_dim += args.atom_descriptors_size

        dropout = nn.Dropout(args.dropout)
        activation = get_activation_function(args.activation)

        # Create FFN layers
        if args.ffn_num_layers[0] > 0:
            shared_layers = [dropout, nn.Linear(first_linear_dim,args.ffn_hidden_size[0])]
            for i in range(args.ffn_num_layers[0]-1):
                shared_layers.extend([
                              activation,
                              dropout,
                              nn.Linear(args.ffn_hidden_size[i], args.ffn_hidden_size[i+1]),
                ])
            self.shared_layers = nn.Sequential(*shared_layers)
            shared_dimension = args.ffn_hidden_size[args.ffn_num_layers[0] - 1]
        else:
            self.shared_layers = None
            shared_dimension = first_linear_dim
        multi_branch_ffn = []
        for branch in args.ffn_hidden_size[args.ffn_num_layers[0]:]:
            ffn = [
                  dropout, 
                  nn.Linear(shared_dimension, branch[0])
            ]
            for i in range(len(branch) - 1):
                ffn.extend([
                    activation,
                    dropout,
                    nn.Linear(branch[i], branch[i+1]),
                ])
            ffn.extend([
                activation,
                dropout,
                nn.Linear(branch[-1], 1),
            ])
            multi_branch_ffn.append(nn.Sequential(*ffn))
        self.ffn = nn.ModuleList(multi_branch_ffn)

    def create_ffn(self, args: TrainArgs) -> None:
        """
        Creates the feed-forward layers for the model.

        :param args: A :class:`~chemprop.args.TrainArgs` object containing model arguments.
        """
        self.multiclass = args.dataset_type == 'multiclass'
        if self.multiclass:
            self.num_classes = args.multiclass_num_classes
        if args.features_only:
            first_linear_dim = (args.features_size + self.extra_features)
        else:
            first_linear_dim = args.hidden_size * args.number_of_molecules
            if args.use_input_features:
                first_linear_dim += (args.features_size + self.extra_features)

        if args.atom_descriptors == 'descriptor':
            first_linear_dim += args.atom_descriptors_size

        dropout = nn.Dropout(args.dropout)
        activation = get_activation_function(args.activation)

        # Create FFN layers
        if args.ffn_num_layers == 1:
            ffn = [
                dropout,
                nn.Linear(first_linear_dim, self.output_size
                                      + self.extra_outputs)
            ]
        else:
            ffn = [
                dropout,
                nn.Linear(first_linear_dim, args.ffn_hidden_size)
            ]
            for _ in range(args.ffn_num_layers - 2):
                ffn.extend([
                    activation,
                    dropout,
                    nn.Linear(args.ffn_hidden_size, args.ffn_hidden_size),
                ])
            ffn.extend([
                activation,
                dropout,
                nn.Linear(args.ffn_hidden_size, self.output_size
                                      + self.extra_outputs),
            ])

        # If spectra model, also include spectra activation
        if args.dataset_type == 'spectra':
            if args.spectra_activation == 'softplus':
                spectra_activation = nn.Softplus()
            else: # default exponential activation which must be made into a custom nn module
                class nn_exp(torch.nn.Module):
                    def __init__(self):
                        super(nn_exp, self).__init__()
                    def forward(self, x):
                        return torch.exp(x)
                spectra_activation = nn_exp()
            ffn.append(spectra_activation)

        # Create FFN model
        self.ffn = nn.Sequential(*ffn)
        
        if args.checkpoint_frzn is not None:
            if args.frzn_ffn_layers >0:
                for param in list(self.ffn.parameters())[0:2*args.frzn_ffn_layers]: # Freeze weights and bias for given number of layers
                    param.requires_grad=False

    def fingerprint(self,
                  batch: Union[List[List[str]], List[List[Chem.Mol]], List[List[Tuple[Chem.Mol, Chem.Mol]]], List[BatchMolGraph]],
                  features_batch: List[np.ndarray] = None,
                  atom_descriptors_batch: List[np.ndarray] = None,
                  atom_features_batch: List[np.ndarray] = None,
                  bond_features_batch: List[np.ndarray] = None,
                  fingerprint_type = 'MPN') -> torch.FloatTensor:
        """
        Encodes the latent representations of the input molecules from intermediate stages of the model. 

        :param batch: A list of list of SMILES, a list of list of RDKit molecules, or a
                      list of :class:`~chemprop.features.featurization.BatchMolGraph`.
                      The outer list or BatchMolGraph is of length :code:`num_molecules` (number of datapoints in batch),
                      the inner list is of length :code:`number_of_molecules` (number of molecules per datapoint).
        :param features_batch: A list of numpy arrays containing additional features.
        :param atom_descriptors_batch: A list of numpy arrays containing additional atom descriptors.
        :param fingerprint_type: The choice of which type of latent representation to return as the molecular fingerprint. Currently 
                                 supported MPN for the output of the MPNN portion of the model or last_FFN for the input to the final readout layer.
        :return: The latent fingerprint vectors.
        """
        if fingerprint_type == 'MPN':
            return self.encoder(batch, features_batch, atom_descriptors_batch,
                                      atom_features_batch, bond_features_batch)
        elif fingerprint_type == 'last_FFN':
            return self.ffn[:-1](self.encoder(batch, features_batch, atom_descriptors_batch,
                                            atom_features_batch, bond_features_batch))
        else:
            raise ValueError(f'Unsupported fingerprint type {fingerprint_type}.')

    def forward(self,
                batch: Union[List[List[str]], List[List[Chem.Mol]], List[List[Tuple[Chem.Mol, Chem.Mol]]], List[BatchMolGraph]],
                features_batch: List[np.ndarray] = None,
                atom_descriptors_batch: List[np.ndarray] = None,
                atom_features_batch: List[np.ndarray] = None,
                bond_features_batch: List[np.ndarray] = None) -> torch.FloatTensor:
        """
        Runs the :class:`MoleculeModel` on input.

        :param batch: A list of list of SMILES, a list of list of RDKit molecules, or a
                      list of :class:`~chemprop.features.featurization.BatchMolGraph`.
                      The outer list or BatchMolGraph is of length :code:`num_molecules` (number of datapoints in batch),
                      the inner list is of length :code:`number_of_molecules` (number of molecules per datapoint).
        :param features_batch: A list of numpy arrays containing additional features.
        :param atom_descriptors_batch: A list of numpy arrays containing additional atom descriptors.
        :param atom_features_batch: A list of numpy arrays containing additional atom features.
        :param bond_features_batch: A list of numpy arrays containing additional bond features.
        :return: The output of the :class:`MoleculeModel`, containing a list of property predictions
        """
        if self.custom_func is not None:
            custom_input, features_batch = self.modify_features(features_batch)
            if len(features_batch[0]) == 1:  
                features_batch = None
                self.encoder.use_input_features = False
        
        if self.is_multi_branch == True:
            if self.shared_layers is not None:
                encoder_output = self.shared_layers(self.encoder(batch
                                    , features_batch, atom_descriptors_batch,
                                     atom_features_batch, bond_features_batch))
            else:
                encoder_output = self.encoder(batch
                                    , features_batch, atom_descriptors_batch,
                                     atom_features_batch, bond_features_batch)
            output = []
            for branch in self.ffn:
                output.append(branch(encoder_output))
            output = torch.cat(output, dim=1)
        else:
            output = self.ffn(self.encoder(batch, features_batch
                                           , atom_descriptors_batch
                                           ,atom_features_batch
                                           , bond_features_batch))
        
        if self.custom_func is not None:
            output = self.custom_func(output, custom_input)
        
        # Don't apply sigmoid during training b/c using BCEWithLogitsLoss
        if self.classification and not self.training:
            output = self.sigmoid(output)
        if self.multiclass:
            output = output.reshape((output.size(0), -1, self.num_classes))  # batch size x num targets x num classes per target
            if not self.training:
                output = self.multiclass_softmax(output)  # to get probabilities during evaluation, but not during training as we're using CrossEntropyLoss
        
        return output