#
# This file is part of LiteX-M2SDR.
#
# SPDX-License-Identifier: BSD-2-Clause

# Capability gateware version / build-info CSRs: values, and appended after the original CSRs.

from migen import *

from litex_m2sdr.gateware.capability import Capability, GATEWARE_VERSION, gateware_version_word

def make_capability(**kw):
    return Capability(api_version_str="1.0",
        pcie_enabled=True, pcie_speed="gen2", pcie_lanes=4, pcie_ptm=False,
        eth_enabled=False, eth_speed="1000basex", eth_ptp=False, eth_ptp_rfic_clock=False,
        sata_enabled=False, sata_gen="gen2", sata_mode="read+write",
        gpio_enabled=False, wr_enabled=False, timed_tx=True,
        variant="m2", jtagbone=False, eth_sfp=0, wr_sfp=0, **kw)

def test_version_word_packing():
    assert gateware_version_word("1.1.0") == 0x010100
    assert gateware_version_word("1.1.7") == 0x010107
    assert gateware_version_word("2.0.255") == 0x0200ff
    # Patch bumps keep integer ordering.
    assert gateware_version_word("1.1.9") < gateware_version_word("1.1.10") < gateware_version_word("1.2.0")

def test_version_csrs_read_back():
    cap = make_capability(git_hash=0x1234abcd, git_dirty=True, build_time=1794847903)
    st = {}

    def gen():
        st["version"] = (yield cap._gateware_version.status)
        st["major"]   = (yield cap._gateware_version.fields.major)
        st["minor"]   = (yield cap._gateware_version.fields.minor)
        st["patch"]   = (yield cap._gateware_version.fields.patch)
        st["hash"]    = (yield cap._git_hash.status)
        st["dirty"]   = (yield cap._build_info.fields.git_dirty)
        st["time"]    = (yield cap._build_time.status)

    run_simulation(cap, gen())
    major, minor, patch = map(int, GATEWARE_VERSION.split("."))
    assert st["version"] == gateware_version_word(GATEWARE_VERSION)
    assert (st["major"], st["minor"], st["patch"]) == (major, minor, patch)
    assert st["hash"] == 0x1234abcd and st["dirty"] == 1 and st["time"] == 1794847903

def test_new_csrs_are_appended():
    # Software of older images hard-codes the first six capability CSR positions.
    names = [c.name for c in make_capability().get_csrs()]
    assert names[:6] == ["api_version", "features", "pcie_config", "eth_config", "sata_config", "board_info"]
    assert names[6:] == ["gateware_version", "git_hash", "build_info", "build_time"]
