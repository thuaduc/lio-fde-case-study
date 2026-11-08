from scripts.eval_extraction import money_ok, tax_id_ok, totals_ok, vendor_ok


def test_vendor_is_case_insensitive_substring():
    assert vendor_ok("GreenMantle Moss Co.", ("greenmantle",))
    assert vendor_ok("BloomCraft Inc", ("verdeform", "bloomcraft"))
    assert not vendor_ok("Acme Corp", ("hollis",))
    assert not vendor_ok(None, ("hollis",))


def test_tax_id_compares_digits_only():
    assert tax_id_ok("85-3252405", "853252405")
    assert tax_id_ok("EIN 94-2404110", "942404110")
    assert tax_id_ok("", None)
    assert not tax_id_ok("12-3456789", None)
    assert not tax_id_ok("", "853252405")


def test_money_within_a_cent():
    assert money_ok(1686.14, 1686.14)
    assert money_ok(1686.15, 1686.14)
    assert not money_ok(1686.16, 1686.14)
    assert not money_ok(None, 1.0)


def test_totals_compare_as_multisets():
    assert totals_ok([622.0, 715.26, 215.0], [715.26, 622.0, 215.0])
    assert not totals_ok([622.0, 715.26], [715.26, 622.0, 215.0])
    assert not totals_ok([622.0, 715.26, 216.0], [715.26, 622.0, 215.0])
