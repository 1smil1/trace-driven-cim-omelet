"""gem5-side assertions used by test_online_bridge's native-state check."""

from pathlib import Path
import os
import runpy
import sys

import m5
from m5.objects import ClockedObject, MessageBuffer, Root, RubyDirectoryMemory
from m5.params import EthernetAddr, Int, isNullPointer
from m5.proxy import Parent, isproxy


original_instantiate = m5.instantiate


def checked_instantiate(*args, **kwargs):
    if "OMELET_TEST_NATIVE_BUFFER_CONFLICT" in os.environ:
        assert os.environ["OMELET_TEST_NATIVE_BUFFER_CONFLICT"] == "1"
        controller = Root.getInstance().system.ruby.l1_cntrl1
        assert not isNullPointer(controller.native_message_buffer_template)
        controller.mandatoryQueue = MessageBuffer()
    assert isproxy(Parent.any)
    assert isproxy([0, (EthernetAddr("00:90:00:00:00:01"),)])
    assert not isproxy([Int(4), (None, "plain", 2.5)])
    assert Parent.width._opcheck(7, None) == 7
    assert (Parent.width * 3)._opcheck(7, None) == 21
    if "OMELET_TEST_ROUTE_WEIGHT" in os.environ:
        weight = int(os.environ["OMELET_TEST_ROUTE_WEIGHT"])
        network = Root.getInstance().system.ruby.network
        if weight:
            for link in [*network.int_links, *network.ext_links]:
                link.weight = weight
    if "OMELET_TEST_DISTINCT_VNETS" in os.environ:
        assert os.environ["OMELET_TEST_DISTINCT_VNETS"] == "1"
        Root.getInstance().system.ruby.network.int_links[0].supported_vnets = [1, 2]
    if "OMELET_TEST_DISTINCT_ENDPOINTS" in os.environ:
        network = Root.getInstance().system.ruby.network
        directories = [link for link in network.ext_links
                       if link.ext_node.type == "Directory_Controller"]
        assert directories
        directories[0].weight = int(directories[0].weight) + 1
    if "OMELET_TEST_NATIVE_EXPORT" in os.environ:
        # Class defaults and topology have already selected the normal native
        # path. Enable display metadata/export solely for this small regression.
        os.environ["OMELET_COMM_DUMP_STATS"] = "1"
    original_instantiate(*args, **kwargs)
    assert ClockedObject.native_power_state
    system = Root.getInstance().system
    assert system.online_memory and all(bool(memory.null) for memory in system.online_memory)
    directories = [obj for obj in system.descendants()
                   if isinstance(obj, RubyDirectoryMemory)]
    assert directories and all(bool(obj.lazy_entries) for obj in directories)
    controllers = [obj for obj in system.descendants()
                   if obj.type in ("L1Cache_Controller", "Directory_Controller")]
    queue_ids = []
    for controller in controllers:
        if isNullPointer(controller.native_message_buffer_template):
            continue
        names = (("mandatoryQueue", "requestFromCache", "forwardFromCache",
                  "responseFromCache") if controller.type == "L1Cache_Controller"
                 else ("requestToDir", "forwardToDir", "responseToDir"))
        groups = controller.getCCObject().getStatGroups()
        for name in names:
            assert name not in controller._children
            assert name in groups
            ids = {stat.id for stat in groups[name].getStats()}
            assert ids
            assert all(ids.isdisjoint(previous) for previous in queue_ids)
            queue_ids.append(ids)
    assert queue_ids
    links = system.ruby.network.int_links
    assert any(parent.native_children for parent in links)
    states = []
    for parent in links[:2]:
        if parent.native_children:
            assert "network_link" not in parent._children
            assert "credit_link" not in parent._children
            groups = parent.getCCObject().getStatGroups()
            assert "credit_link" in groups
            link_group = groups["network_link"]
        else:
            link = parent.network_link
            assert "power_state" not in link._children
            link_group = link.getCCObject()
        states.append(link_group.getStatGroups()["power_state"])
    assert states[0] is not states[1]
    bridged = [parent for parent in links
               if parent.src_cdc or parent.src_serdes or parent.dst_cdc or parent.dst_serdes]
    assert bridged
    for parent in bridged:
        assert parent.native_children
        groups = parent.getCCObject().getStatGroups()
        for side in ("src", "dst"):
            if bool(getattr(parent, side + "_cdc")) or bool(getattr(parent, side + "_serdes")):
                for suffix in ("net_bridge", "cred_bridge"):
                    name = side + "_" + suffix
                    assert name not in parent._children
                    assert name in groups
                    states.append(groups[name].getStatGroups()["power_state"])
    external = system.ruby.network.ext_links[0]
    assert external.native_children
    assert not external.network_links and not external.credit_links
    groups = external.getCCObject().getStatGroups()
    child_names = {"network_links0", "network_links1", "credit_links0", "credit_links1"}
    assert child_names <= set(groups), set(groups)
    states.extend(groups[name].getStatGroups()["power_state"] for name in sorted(child_names))
    template = system.online_power_state_template.getCCObject()
    assert all(state is not template for state in states)
    stat_ids = [{stat.id for stat in state.getStats()} for state in states]
    assert all(stat_ids)
    for index, ids in enumerate(stat_ids):
        assert all(ids.isdisjoint(other) for other in stat_ids[:index])
    print("Native PowerState check: independent states and statistics verified", flush=True)


m5.instantiate = checked_instantiate
config = Path(__file__).resolve().parents[1] / "gem5/configs/example/garnet_synth_traffic.py"
sys.path.insert(0, str(config.parent.parent))
runpy.run_path(str(config), run_name="__main__")
