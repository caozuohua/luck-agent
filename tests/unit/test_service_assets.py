from core.service_assets import format_assets, parse_assets


def test_parse_assets_and_format():
    assets = parse_assets('{"assets":[{"asset_type":"systemd","service_id":"luck-agent","status":"active","ports":[8000]}]}')
    assert assets[0].service_id == "luck-agent"
    assert "luck-agent" in format_assets(assets)
    assert "8000" in format_assets(assets)


def test_parse_empty_assets():
    assert parse_assets("[]") == ()
    assert "未发现" in format_assets(())
