"""Static checks for the README user-facing documentation contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"
HACS_REPOSITORY_URL = (
    "https://my.home-assistant.io/redirect/hacs_repository/"
    "?owner=cash83&repository=cudy-router-ha-control&category=integration"
)


def _readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


def test_readme_is_a_concise_landing_page() -> None:
    """README should stay short enough to work as a quick-start page."""
    source = _readme()

    assert source.startswith("# Cudy Router HA Control")
    assert len(source.splitlines()) < 150
    assert "This project is not endorsed, maintained, or supported by Cudy." in source
    assert "## Tested Focus" in source


def test_readme_has_direct_hacs_my_home_assistant_button() -> None:
    """README should offer a one-click HACS repository link."""
    source = _readme()

    assert "https://my.home-assistant.io/badges/hacs_repository.svg" in source
    assert HACS_REPOSITORY_URL in source
    assert 'target="_blank"' in source
    assert 'rel="noopener noreferrer"' in source
    assert "owner=cash83" in source
    assert "repository=cudy-router-ha-control" in source
    assert "category=integration" in source


def test_readme_documents_hacs_custom_repository_install() -> None:
    """README should include explicit HACS custom repository fallback steps."""
    source = _readme()

    assert "Custom repositories" in source
    assert "https://github.com/cash83/cudy-router-ha-control" in source
    assert "Integration repository" in source or "**Integration** repository" in source
    assert "Restart Home Assistant" in source


def test_readme_keeps_current_supported_status_visible() -> None:
    """README should keep the real-hardware support status visible."""
    source = _readme()

    assert "Cudy P4 / P4 5G firmware family" in source
    assert "Cudy M3000 firmware family" in source
    assert "Other Cudy models may work" in source


def test_readme_summarizes_major_user_facing_features() -> None:
    """README should summarize the integration surface without full reference detail."""
    source = _readme()

    assert "WAN, LAN, modem/cellular, DHCP, Wi-Fi, VPN" in source
    assert "Switch and select entities" in source
    assert "Connected-client controls" in source
    assert "cudy_router.reboot_router" in source
    assert "cudy_router.restart_5g_connection" in source
    assert "cudy_router.switch_5g_band" in source
    assert "cudy_router.send_sms" in source
    assert "cudy_router.send_at_command" in source


def test_readme_links_directly_to_bug_report_issue_form() -> None:
    """README should send users straight to the diagnostics-aware bug form."""
    source = _readme()

    assert (
        "https://github.com/cash83/cudy-router-ha-control/issues/new"
        "?template=bug_report.yml"
    ) in source
    assert "Home Assistant version" in source
    assert "Integration version" in source
    assert "Router model and firmware" in source
    assert "The entity that failed" in source
