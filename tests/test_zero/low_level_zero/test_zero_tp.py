from functools import partial

import pytest
import torch
import torch.multiprocessing as mp
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.testing import assert_close

import colossalai
from colossalai.tensor import ProcessGroup
from colossalai.testing import parameterize, rerun_if_address_is_in_use
from colossalai.utils import free_port, get_current_device
from colossalai.utils.model.colo_init_context import ColoInitContext
from colossalai.zero import LowLevelZeroOptimizer
from tests.test_tensor.common_utils import set_seed, split_param_col_tp1d, split_param_row_tp1d, tensor_shard_equal


def strict_shard_equal(tensor, shard, tp_pg, rtol=1e-3, atol=1e-4):
    return tensor_shard_equal(tensor, shard, tp_pg.tp_local_rank(), tp_pg.tp_world_size(), rtol, atol)


class TestModel(nn.Module):

    def __init__(self):
        super(TestModel, self).__init__()
        self.linear1 = nn.Linear(32, 128)
        self.act = nn.GELU()
        self.linear2 = nn.Linear(128, 32)

    def forward(self, x):
        y = self.linear1(x)
        y = self.act(y)
        y = self.linear2(y)
        return x + y


@parameterize("overlap_flag", [False, True])
@parameterize("partition_flag", [False, True])
def exam_zero_with_tp(overlap_flag, partition_flag):
    set_seed(233010)
    tp_pg = ProcessGroup(tp_degree=2)

    with ColoInitContext(device=get_current_device(), default_pg=tp_pg):
        hybrid_model = TestModel()
    torch_model = TestModel().cuda()
    for pt, ph in zip(torch_model.parameters(), hybrid_model.parameters()):
        pt.data.copy_(ph.data)

    for name, param in hybrid_model.named_parameters():
        if 'linear1' in name:
            split_param_row_tp1d(param, tp_pg)
            param.compute_spec.set_output_replicate(False)
        if 'linear2.weight' in name:
            split_param_col_tp1d(param, tp_pg)

    torch_model = DDP(torch_model, device_ids=[tp_pg.rank()], process_group=tp_pg.dp_process_group())
    torch_optim = torch.optim.Adam(torch_model.parameters(), lr=1)
    hybrid_optim = torch.optim.Adam(hybrid_model.parameters(), lr=1)
    hybrid_optim = LowLevelZeroOptimizer(hybrid_optim,
                                         initial_scale=1,
                                         overlap_communication=overlap_flag,
                                         partition_grad=partition_flag)

    dp_local_rank = tp_pg.dp_local_rank()
    set_seed(255 + dp_local_rank)

    data = torch.randn(8, 32, device=get_current_device())
    torch_loss = torch_model(data).sum()
    hybrid_loss = hybrid_model(data).sum()
    assert_close(torch_loss, hybrid_loss)

    torch_loss.backward()
    hybrid_optim.backward(hybrid_loss)
    hybrid_optim.sync_grad()

    torch_optim.step()
    hybrid_optim.step()

    for (name, pt), ph in zip(torch_model.named_parameters(), hybrid_model.parameters()):
        assert strict_shard_equal(pt.data, ph.data, tp_pg)


def run_dist(rank, world_size, port):
    colossalai.launch(config={}, rank=rank, world_size=world_size, port=port, host='localhost')
    exam_zero_with_tp()


@pytest.mark.dist
@rerun_if_address_is_in_use()
def test_zero_with_tp():
    world_size = 4
    run_func = partial(run_dist, world_size=world_size, port=free_port())
    mp.spawn(run_func, nprocs=world_size)


if __name__ == '__main__':
    test_zero_with_tp()
