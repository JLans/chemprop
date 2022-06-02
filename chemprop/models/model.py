from typing import List, Union, Tuple

import numpy as np
from rdkit import Chem
import torch
import torch.nn as nn

from .mpn import MPN
from .ffn import DenseLayers, MultiReadout
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
        self.loss_function = args.loss_function

        if hasattr(args, 'train_class_sizes'):
            self.train_class_sizes = args.train_class_sizes
        else:
            self.train_class_sizes = None

        # when using cross entropy losses, no sigmoid or softmax during training. But they are needed for mcc loss.
        if self.classification or self.multiclass:
            self.no_training_normalization = args.loss_function in ['cross_entropy', 'binary_cross_entropy']

        self.is_atom_bond_targets = args.is_atom_bond_targets

        if self.is_atom_bond_targets:
            self.atom_targets, self.bond_targets = args.atom_targets, args.bond_targets
            self.atom_constraints, self.bond_constraints = args.atom_constraints, args.bond_constraints

        self.relative_output_size = 1
        if self.multiclass:
            self.relative_output_size *= args.multiclass_num_classes
        if self.loss_function == 'mve':
            self.relative_output_size *= 2  # return means and variances
        if self.loss_function == 'dirichlet' and self.classification:
            self.relative_output_size *= 2  # return dirichlet parameters for positive and negative class
        if self.loss_function == 'evidential':
            self.relative_output_size *= 4  # return four evidential parameters: gamma, lambda, alpha, beta

        if self.classification:
            self.sigmoid = nn.Sigmoid()

        if self.multiclass:
            self.multiclass_softmax = nn.Softmax(dim=2)

        if self.loss_function in ['mve', 'evidential', 'dirichlet']:
            self.softplus = nn.Softplus()

        self.create_encoder(args)
        self.create_ffn(args)

        initialize_weights(self)

    def create_encoder(self, args: TrainArgs) -> None:
        """
        Creates the message passing encoder for the model.

        :param args: A :class:`~chemprop.args.TrainArgs` object containing model arguments.
        """
        self.encoder = MPN(args)

        if args.checkpoint_frzn is not None:
            if args.freeze_first_only:  # Freeze only the first encoder
                for param in list(self.encoder.encoder.children())[0].parameters():
                    param.requires_grad = False
            else:  # Freeze all encoders
                for param in self.encoder.parameters():
                    param.requires_grad = False

    def create_ffn(self, args: TrainArgs) -> None:
        """
        Creates the feed-forward layers for the model.

        :param args: A :class:`~chemprop.args.TrainArgs` object containing model arguments.
        """
        self.multiclass = args.dataset_type == 'multiclass'
        if self.multiclass:
            self.num_classes = args.multiclass_num_classes
        if args.features_only:
            first_linear_dim = args.features_size
        else:
            if args.reaction_solvent:
                first_linear_dim = args.hidden_size + args.hidden_size_solvent
            else:
                first_linear_dim = args.hidden_size * args.number_of_molecules
            if args.use_input_features:
                first_linear_dim += args.features_size

        if args.atom_descriptors == 'descriptor':
            first_linear_dim += args.atom_descriptors_size

        dropout = nn.Dropout(args.dropout)
        activation = get_activation_function(args.activation)

        # Create FFN layers
        if self.is_atom_bond_targets:
            self.readout = MultiReadout(features_size=first_linear_dim,
                                        hidden_size=args.ffn_hidden_size,
                                        num_layers=args.ffn_num_layers,
                                        output_size=self.relative_output_size,
                                        dropout=dropout,
                                        activation=activation,
                                        atom_constraints=args.atom_constraints,
                                        bond_constraints=args.bond_constraints,
                                        shared_ffn=args.shared_atom_bond_ffn,
                                        weights_ffn_num_layers=args.weights_ffn_num_layers)
        else:
            self.readout = DenseLayers(first_linear_dim=first_linear_dim,
                                       hidden_size=args.ffn_hidden_size,
                                       num_layers=args.ffn_num_layers,
                                       output_size=self.relative_output_size * args.num_tasks,
                                       dropout=dropout,
                                       activation=activation,
                                       dataset_type=args.dataset_type,
                                       spectra_activation=args.spectra_activation)

        if args.checkpoint_frzn is not None:
            if args.frzn_ffn_layers > 0:
                for param in list(self.readout.dense_layers.parameters())[0:2 * args.frzn_ffn_layers]:  # Freeze weights and bias for given number of layers
                    param.requires_grad = False

    def fingerprint(self,
                    batch: Union[List[List[str]], List[List[Chem.Mol]], List[List[Tuple[Chem.Mol, Chem.Mol]]], List[BatchMolGraph]],
                    features_batch: List[np.ndarray] = None,
                    atom_descriptors_batch: List[np.ndarray] = None,
                    atom_features_batch: List[np.ndarray] = None,
                    bond_features_batch: List[np.ndarray] = None,
                    fingerprint_type: str = 'MPN') -> torch.Tensor:
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
            return self.readout.dense_layers[:-1](self.encoder(batch, features_batch, atom_descriptors_batch,
                                                  atom_features_batch, bond_features_batch))
        else:
            raise ValueError(f'Unsupported fingerprint type {fingerprint_type}.')

    def forward(self,
                batch: Union[List[List[str]], List[List[Chem.Mol]], List[List[Tuple[Chem.Mol, Chem.Mol]]], List[BatchMolGraph]],
                features_batch: List[np.ndarray] = None,
                atom_descriptors_batch: List[np.ndarray] = None,
                atom_features_batch: List[np.ndarray] = None,
                bond_features_batch: List[np.ndarray] = None,
                constraints_batch: List[torch.tensor] = None) -> torch.FloatTensor:
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
        :param constraints_batch: A list of torch.tensor which applies constraint on atomic/bond properties.
        :return: The output of the :class:`MoleculeModel`, containing a list of property predictions
        """
        if self.is_atom_bond_targets:
            encodings = self.encoder(batch, features_batch, atom_descriptors_batch,
                                     atom_features_batch, bond_features_batch)
            output = self.readout(encodings, constraints_batch)
        else:
            encodings = self.encoder(batch, features_batch, atom_descriptors_batch,
                                     atom_features_batch, bond_features_batch)
            output = self.readout(encodings)

        # Don't apply sigmoid during training when using BCEWithLogitsLoss
        if self.classification and not (self.training and self.no_training_normalization) and self.loss_function != 'dirichlet':
            if self.is_atom_bond_targets:
                output = [self.sigmoid(x) for x in output]
            else:
                output = self.sigmoid(output)
        if self.multiclass:
            output = output.reshape((output.shape[0], -1, self.num_classes))  # batch size x num targets x num classes per target
            if not (self.training and self.no_training_normalization) and self.loss_function != 'dirichlet':
                output = self.multiclass_softmax(output)  # to get probabilities during evaluation, but not during training when using CrossEntropyLoss

        # Modify multi-input loss functions
        if self.loss_function == 'mve':
            def get_mve_output(output):
                means, variances = torch.split(output, output.shape[1] // 2, dim=1)
                variances = self.softplus(variances)
                return torch.cat([means, variances], axis=1)
            if self.is_atom_bond_targets:
                output = [get_mve_output(x) for x in output]
            else:
                output = get_mve_output(output)
        if self.loss_function == 'evidential':
            def get_evidential_output(output):
                means, lambdas, alphas, betas = torch.split(output, output.shape[1]//4, dim=1)
                lambdas = self.softplus(lambdas)  # + min_val
                alphas = self.softplus(alphas) + 1  # + min_val # add 1 for numerical contraints of Gamma function
                betas = self.softplus(betas)  # + min_val
                return torch.cat([means, lambdas, alphas, betas], dim=1)
            if self.is_atom_bond_targets:
                output = [get_evidential_output(x) for x in output]
            else:
                output = get_evidential_output(output)
        if self.loss_function == 'dirichlet':
            def get_dirichlet_output(output):
                return nn.functional.softplus(output) + 1
            if self.is_atom_bond_targets:
                output = [get_dirichlet_output(x) for x in output]
            else:
                output = get_dirichlet_output(output)

        return output
