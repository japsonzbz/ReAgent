#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.

import logging
from typing import Dict, List, Optional, Tuple

import reagent.types as rlt
import torch
import torch.nn.functional as F
from reagent.models.base import ModelBase
from reagent.models.seq2slate import Seq2SlateMode, Seq2SlateTransformerNet
from reagent.preprocessing.postprocessor import Postprocessor
from reagent.preprocessing.preprocessor import Preprocessor
from torch import nn


logger = logging.getLogger(__name__)


# TODO: The feature definition should be ModelFeatureConfig


class DiscreteDqnWithPreprocessor(ModelBase):
    """
    This is separated from DiscreteDqnPredictorWrapper so that we can pass typed inputs
    into the model. This is possible because JIT only traces tensor operation.
    In contrast, JIT scripting needs to compile the code, therefore, it won't recognize
    any custom Python type.
    """

    def __init__(self, model: ModelBase, state_preprocessor: Preprocessor):
        super().__init__()
        self.model = model
        self.state_preprocessor = state_preprocessor

    def forward(self, state_with_presence: Tuple[torch.Tensor, torch.Tensor]):
        preprocessed_state = self.state_preprocessor(
            state_with_presence[0], state_with_presence[1]
        )
        state_feature_vector = rlt.FeatureData(preprocessed_state)
        q_values = self.model(state_feature_vector)
        return q_values

    def input_prototype(self):
        return (self.state_preprocessor.input_prototype(),)


class DiscreteDqnWithPreprocessorWithIdList(ModelBase):
    """
    This is separated from DiscreteDqnPredictorWrapper so that we can pass typed inputs
    into the model. This is possible because JIT only traces tensor operation.
    In contrast, JIT scripting needs to compile the code, therefore, it won't recognize
    any custom Python type.
    """

    def __init__(
        self,
        model: ModelBase,
        state_preprocessor: Preprocessor,
        state_feature_config: Optional[rlt.ModelFeatureConfig] = None,
    ):
        super().__init__()
        self.model = model
        self.state_preprocessor = state_preprocessor
        self.state_feature_config = state_feature_config

    def forward(
        self,
        state_with_presence: Tuple[torch.Tensor, torch.Tensor],
        state_id_list_features: Dict[int, Tuple[torch.Tensor, torch.Tensor]],
    ):
        preprocessed_state = self.state_preprocessor(
            state_with_presence[0], state_with_presence[1]
        )
        id_list_features = {
            id_list_feature_config.name: state_id_list_features[
                id_list_feature_config.feature_id
            ]
            for id_list_feature_config in self.id_list_feature_configs
        }
        state_feature_vector = rlt.FeatureData(
            float_features=preprocessed_state, id_list_features=id_list_features
        )
        q_values = self.model(state_feature_vector)
        return q_values

    @property
    def id_list_feature_configs(self) -> List[rlt.IdListFeatureConfig]:
        if self.state_feature_config:
            # pyre-fixme[16]: `Optional` has no attribute `id_list_feature_configs`.
            return self.state_feature_config.id_list_feature_configs
        return []

    def input_prototype(self):
        feature_name_to_id = {
            config.name: config.feature_id for config in self.id_list_feature_configs
        }
        state_id_list_features = {
            feature_name_to_id[k]: v
            for k, v in self.model.input_prototype().id_list_features.items()
        }
        # Terrible hack to make JIT tracing works. Python dict doesn't have type
        # so we need to insert something so JIT tracer can infer the type.
        if not state_id_list_features:
            state_id_list_features = {
                -1: (
                    torch.zeros(1, dtype=torch.long),
                    torch.tensor([], dtype=torch.long),
                )
            }
        return (self.state_preprocessor.input_prototype(), state_id_list_features)


class DiscreteDqnPredictorWrapper(torch.jit.ScriptModule):
    def __init__(
        self,
        dqn_with_preprocessor: DiscreteDqnWithPreprocessor,
        action_names: List[str],
        state_feature_config: Optional[rlt.ModelFeatureConfig] = None,
    ) -> None:
        """
        state_feature_config is here to keep the interface consistent with FB internal
        version
        """
        super().__init__()

        self.dqn_with_preprocessor = torch.jit.trace(
            dqn_with_preprocessor, dqn_with_preprocessor.input_prototype()
        )
        self.action_names = torch.jit.Attribute(action_names, List[str])

    @torch.jit.script_method
    def forward(
        self, state_with_presence: Tuple[torch.Tensor, torch.Tensor]
    ) -> Tuple[List[str], torch.Tensor]:
        q_values = self.dqn_with_preprocessor(state_with_presence)
        return (self.action_names, q_values)


# Pass through serving module's output
class OSSPredictorUnwrapper(nn.Module):
    def __init__(self, model: nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, *args, **kwargs) -> Tuple[List[str], torch.Tensor]:
        return self.model(*args, **kwargs)


DiscreteDqnPredictorUnwrapper = OSSPredictorUnwrapper
ActorPredictorUnwrapper = OSSPredictorUnwrapper
ParametricDqnPredictorUnwrapper = OSSPredictorUnwrapper


class DiscreteDqnPredictorWrapperWithIdList(torch.jit.ScriptModule):
    def __init__(
        self,
        dqn_with_preprocessor: DiscreteDqnWithPreprocessorWithIdList,
        action_names: List[str],
        state_feature_config: Optional[rlt.ModelFeatureConfig] = None,
    ) -> None:
        """
        state_feature_config is here to keep the interface consistent with FB internal
        version
        """
        super().__init__()

        self.dqn_with_preprocessor = torch.jit.trace(
            dqn_with_preprocessor, dqn_with_preprocessor.input_prototype()
        )
        self.action_names = torch.jit.Attribute(action_names, List[str])

    @torch.jit.script_method
    def forward(
        self,
        state_with_presence: Tuple[torch.Tensor, torch.Tensor],
        state_id_list_features: Dict[int, Tuple[torch.Tensor, torch.Tensor]],
    ) -> Tuple[List[str], torch.Tensor]:
        q_values = self.dqn_with_preprocessor(
            state_with_presence, state_id_list_features
        )
        return (self.action_names, q_values)


class ParametricDqnWithPreprocessor(ModelBase):
    def __init__(
        self,
        model: ModelBase,
        state_preprocessor: Preprocessor,
        action_preprocessor: Preprocessor,
    ):
        super().__init__()
        self.model = model
        self.state_preprocessor = state_preprocessor
        self.action_preprocessor = action_preprocessor

    def forward(
        self,
        state_with_presence: Tuple[torch.Tensor, torch.Tensor],
        action_with_presence: Tuple[torch.Tensor, torch.Tensor],
    ):
        preprocessed_state = self.state_preprocessor(
            state_with_presence[0], state_with_presence[1]
        )
        preprocessed_action = self.action_preprocessor(
            action_with_presence[0], action_with_presence[1]
        )
        state = rlt.FeatureData(preprocessed_state)
        action = rlt.FeatureData(preprocessed_action)
        q_value = self.model(state, action)
        return q_value

    def input_prototype(self):
        return (
            self.state_preprocessor.input_prototype(),
            self.action_preprocessor.input_prototype(),
        )


class ParametricDqnPredictorWrapper(torch.jit.ScriptModule):
    def __init__(self, dqn_with_preprocessor: ParametricDqnWithPreprocessor) -> None:
        super().__init__()

        self.dqn_with_preprocessor = torch.jit.trace(
            dqn_with_preprocessor, dqn_with_preprocessor.input_prototype()
        )

    @torch.jit.script_method
    def forward(
        self,
        state_with_presence: Tuple[torch.Tensor, torch.Tensor],
        action_with_presence: Tuple[torch.Tensor, torch.Tensor],
    ) -> Tuple[List[str], torch.Tensor]:
        value = self.dqn_with_preprocessor(state_with_presence, action_with_presence)
        return (["Q"], value)


class ActorWithPreprocessor(ModelBase):
    """
    This is separate from ActorPredictorWrapper so that we can pass typed inputs
    into the model. This is possible because JIT only traces tensor operation.
    In contrast, JIT scripting needs to compile the code, therefore, it won't recognize
    any custom Python type.
    """

    def __init__(
        self,
        model: ModelBase,
        state_preprocessor: Preprocessor,
        action_postprocessor: Optional[Postprocessor] = None,
    ):
        super().__init__()
        self.model = model
        self.state_preprocessor = state_preprocessor
        self.action_postprocessor = action_postprocessor

    def forward(self, state_with_presence: Tuple[torch.Tensor, torch.Tensor]):
        preprocessed_state = self.state_preprocessor(
            state_with_presence[0], state_with_presence[1]
        )
        state_feature_vector = rlt.FeatureData(preprocessed_state)
        # TODO: include log_prob in the output
        action = self.model(state_feature_vector).action
        if self.action_postprocessor:
            # pyre-fixme[29]: `Optional[Postprocessor]` is not a function.
            action = self.action_postprocessor(action)
        return action

    def input_prototype(self):
        return (self.state_preprocessor.input_prototype(),)


_DEFAULT_FEATURE_IDS = []


class ActorPredictorWrapper(torch.jit.ScriptModule):
    def __init__(
        self,
        actor_with_preprocessor: ActorWithPreprocessor,
        action_feature_ids: List[int] = _DEFAULT_FEATURE_IDS,
    ) -> None:
        """
        action_feature_ids is here to make the interface consistent with FB internal
        version
        """
        super().__init__()

        self.actor_with_preprocessor = torch.jit.trace(
            actor_with_preprocessor, actor_with_preprocessor.input_prototype()
        )

    @torch.jit.script_method
    def forward(
        self, state_with_presence: Tuple[torch.Tensor, torch.Tensor]
    ) -> torch.Tensor:
        action = self.actor_with_preprocessor(state_with_presence)
        return action


class Seq2SlateWithPreprocessor(ModelBase):
    def __init__(
        self,
        model: Seq2SlateTransformerNet,
        state_preprocessor: Preprocessor,
        candidate_preprocessor: Preprocessor,
        greedy: bool,
    ):
        super().__init__()
        self.model = model
        self.state_preprocessor = state_preprocessor
        self.candidate_preprocessor = candidate_preprocessor
        self.greedy = greedy

    def input_prototype(self):
        candidate_input_prototype = self.candidate_preprocessor.input_prototype()
        return (
            self.state_preprocessor.input_prototype(),
            (
                candidate_input_prototype[0].repeat((1, self.model.max_src_seq_len, 1)),
                candidate_input_prototype[1].repeat((1, self.model.max_src_seq_len, 1)),
            ),
        )

    def forward(
        self,
        state_with_presence: Tuple[torch.Tensor, torch.Tensor],
        candidate_with_presence: Tuple[torch.Tensor, torch.Tensor],
    ):
        # state_value.shape == state_presence.shape == batch_size x state_feat_num
        # candidate_value.shape == candidate_presence.shape ==
        # batch_size x max_src_seq_len x candidate_feat_num
        batch_size = state_with_presence[0].shape[0]

        preprocessed_state = self.state_preprocessor(
            state_with_presence[0], state_with_presence[1]
        )
        preprocessed_candidates = self.candidate_preprocessor(
            candidate_with_presence[0].view(
                batch_size * self.model.max_src_seq_len,
                len(self.candidate_preprocessor.sorted_features),
            ),
            candidate_with_presence[1].view(
                batch_size * self.model.max_src_seq_len,
                len(self.candidate_preprocessor.sorted_features),
            ),
        ).view(batch_size, self.model.max_src_seq_len, -1)

        # TODO: consider different numbers of candidates in the same batch_
        src_src_mask = torch.ones(
            batch_size, self.model.max_src_seq_len, self.model.max_src_seq_len
        )
        ranking_input = rlt.PreprocessedRankingInput.from_tensors(
            state=preprocessed_state,
            src_seq=preprocessed_candidates,
            src_src_mask=src_src_mask,
        )
        ranking_output = self.model(
            ranking_input,
            mode=Seq2SlateMode.RANK_MODE,
            tgt_seq_len=self.model.max_tgt_seq_len,
            greedy=self.greedy,
        )
        return ranking_output.ranked_tgt_out_probs, ranking_output.ranked_tgt_out_idx


class Seq2SlatePredictorWrapper(torch.jit.ScriptModule):
    def __init__(self, seq2slate_with_preprocessor: Seq2SlateWithPreprocessor) -> None:
        super().__init__()
        self.seq2slate_with_preprocessor = torch.jit.trace(
            seq2slate_with_preprocessor, seq2slate_with_preprocessor.input_prototype()
        )

    @torch.jit.script_method
    def forward(
        self,
        state_with_presence: Tuple[torch.Tensor, torch.Tensor],
        candidate_with_presence: Tuple[torch.Tensor, torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # ranked_tgt_out_probs shape: batch_size, tgt_seq_len, candidate_size
        # ranked_tgt_out_idx shape: batch_size, tgt_seq_len
        ranked_tgt_out_probs, ranked_tgt_out_idx = self.seq2slate_with_preprocessor(
            state_with_presence, candidate_with_presence
        )
        # convert to slate-wise probabilities
        # ranked_tgt_out_probs shape: batch_size
        ranked_tgt_out_probs = torch.prod(
            torch.gather(
                ranked_tgt_out_probs, 2, ranked_tgt_out_idx.unsqueeze(-1)
            ).squeeze(),
            -1,
        )
        # -2 to offset padding symbol and decoder start symbol
        ranked_tgt_out_idx -= 2
        return ranked_tgt_out_probs, ranked_tgt_out_idx


class Seq2RewardWithPreprocessor(ModelBase):
    def __init__(
        self,
        model: ModelBase,
        state_preprocessor: Preprocessor,
        seq_len: int,
        num_action: int,
    ):
        """
        Since TorchScript unable to trace control-flow, we
        have to generate the action enumerations as constants
        here so that trace can use them directly.
        """

        super().__init__()
        self.model = model
        self.state_preprocessor = state_preprocessor
        self.seq_len = seq_len
        self.num_action = num_action

        def gen_permutations(seq_len: int, num_action: int) -> torch.Tensor:
            """
            generate all seq_len permutations for a given action set
            the return shape is (SEQ_LEN, PERM_NUM, ACTION_DIM)
            """
            all_permut = torch.cartesian_prod(*[torch.arange(num_action)] * seq_len)
            all_permut = F.one_hot(all_permut, num_action).transpose(0, 1)

            return all_permut.float()

        self.all_permut = gen_permutations(seq_len, num_action)
        self.num_permut = self.all_permut.size(1)

    def forward(self, state_with_presence: Tuple[torch.Tensor, torch.Tensor]):
        """
        This serving module only takes in current state.
        We need to simulate all multi-step length action seq's
        then predict accumulated reward on all those seq's.
        After that, we categorize all action seq's by their
        first actions. Then take the maximum reward as the
        predicted categorical reward for that category.
        Return: categorical reward for the first action
        """
        batch_size, state_dim = state_with_presence[0].size()

        # expand state tensor to match the enumerated action sequences:
        # the tensor manipulations here are tricky:
        # Suppose the input states are s1,s2, these manipulations
        # will generate a input batch s1,s1,...,s1,s2,s2,...,s2
        # where len(s1,s1,...,s1)=len(s2,s2,...,s2)=num_permut
        preprocessed_state = (
            self.state_preprocessor(state_with_presence[0], state_with_presence[1])
            .repeat(1, self.seq_len * self.num_permut)
            .reshape(batch_size * self.num_permut, self.seq_len, state_dim)
            .transpose(0, 1)
        )
        state_feature_vector = rlt.FeatureData(preprocessed_state)

        # expand action to match the expanded state sequence
        action = self.all_permut.repeat(1, batch_size, 1)
        reward = self.model(
            state_feature_vector, rlt.FeatureData(action)
        ).acc_reward.reshape(
            batch_size, self.num_action, self.num_permut // self.num_action
        )

        # The permuations are generated with lexical order
        # the output has shape [num_perm, num_action,1]
        # that means we can aggregate on the max reward
        # then reshape it to (BATCH_SIZE, ACT_DIM)
        max_reward = (
            # pyre-fixme[16]: `Tuple` has no attribute `values`.
            torch.max(reward, 2)
            .values.cpu()
            .detach()
            .reshape(batch_size, self.num_action)
        )

        return max_reward

    def input_prototype(self):
        return (self.state_preprocessor.input_prototype(),)
