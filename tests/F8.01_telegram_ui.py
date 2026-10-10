"""FB6: real release UI in English and French, responsive geometry, conditional sections, drafts and persistence."""

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from playwright.sync_api import sync_playwright, expect, TimeoutError as PlaywrightTimeoutError
from block_test_artifacts import artifact_path
from block_test_packages import install_test_package, surface_payload
from ui_smoke_common import isolated_server, graph_payload, create_project_api, project_editor_url
from blocs.telegram.block import TelegramBlock

VIEWPORTS = ((1440, 900), (800, 700), (390, 740), (320, 568))


def open_modal(card, modal):
    """Open the modal from the canvas card.

    On a loaded host a double-click can land before the editor has bound its handlers
    (notably right after a navigation or a reload): retry until the modal is visible.
    """
    expect(card.locator('h3')).to_be_visible(timeout=30000)
    for _ in range(6):
        if modal.is_visible():
            return
        try:
            card.locator('h3').dblclick(timeout=5000)
            expect(modal).to_be_visible(timeout=5000)
            return
        except (AssertionError, PlaywrightTimeoutError):
            continue
    expect(modal).to_be_visible()


def close_modal(modal, which="first"):
    """Close the modal and wait until it is gone, so a reopen binds to a fresh surface."""
    buttons = modal.locator('[data-close-block-modal]')
    (buttons.last if which == "last" else buttons.first).click()
    expect(modal).to_be_hidden()


def geometry(modal):
    """Require an opaque panel with reachable actions, no page overflow and labelled controls.

    Opening and resize transitions are allowed to settle (bounded) before asserting.
    """
    deadline = time.monotonic() + 5
    while True:
        measured = measure(modal)
        if (measured["inside"] and measured["controls"] and not measured["overflow"]) or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert measured["inside"] and measured["controls"] and not measured["overflow"], measured
    assert not measured["unlabeled"], measured
    assert measured["background"] not in {"transparent", "rgba(0, 0, 0, 0)"}, measured


def measure(modal):
    return modal.evaluate("""el => {
        const r=el.getBoundingClientRect(), body=el.querySelector('.owned-body');
        const actions=[...el.querySelectorAll('[data-close-block-modal], [data-owned-apply]')];
        const visible=[...el.querySelectorAll('input,select,textarea')].filter(c=>c.getClientRects().length);
        return {inside:r.left>=-1&&r.right<=innerWidth+1&&r.top>=-1&&r.bottom<=innerHeight+1,
          overflow:el.scrollWidth>el.clientWidth+2||body.scrollWidth>body.clientWidth+2,
          controls:actions.every(a=>{const b=a.getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight+1}),
          background:getComputedStyle(el).backgroundColor,
          unlabeled:visible.filter(c=>!c.labels?.length&&!c.getAttribute('aria-label')&&!c.getAttribute('aria-labelledby')).length};
    }""")


def main():
    """Exercise current package files in real managed and linked browser hosts."""
    block = TelegramBlock()
    # Managed hosts render surfaces in spawned supervised processes: allow for a busy test host.
    expect.set_options(timeout=15000)
    for origin in ("managed", "linked"):
        with isolated_server() as server, sync_playwright() as playwright:
            model = install_test_package(server, "telegram", origin=origin)
            candidate = block.build_node_payload(node_id="ui-case", position={"x": 160, "y": 160})
            candidate["block_version"] = model["version"]
            for surface in ("modal", "inspector_panel", "node_card"):
                html = surface_payload(server, model, candidate, surface=surface)["html"]
                assert "{{" not in html
            project = create_project_api(server, document=graph_payload("Owned form", [candidate], []))["project"]
            url = project_editor_url(server.base_url, project["project_id"], workspace_project_id=project["workspace_project_id"])
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.add_init_script("window.localStorage.setItem('bloxsmith.inspectorPinned','true')")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                card = page.locator('.canvas-node[data-node-id="ui-case"]')
                expect(card.locator('h3')).to_be_visible()
                expect(card.locator('.node-data-preview')).to_have_text("Remote access off")
                modal = page.locator('.owned-modal')
                open_modal(card, modal)
                expect(modal.locator('[data-owned-apply]')).to_be_disabled()
                receive = modal.locator('[data-block-config-field="receive_updates"]')
                poll = modal.locator('[data-block-config-field="poll_timeout_sec"]')
                expect(poll).to_be_hidden()
                receive.check()
                expect(poll).to_be_visible()
                expect(modal.locator('[data-owned-apply]')).to_be_enabled()
                close_modal(modal, "last")
                open_modal(card, modal)
                expect(receive).not_to_be_checked()
                expect(poll).to_be_hidden()
                # An invalid combination is refused on Apply without changing the saved settings.
                modal.locator('[data-block-config-field="enabled"]').check()
                with page.expect_response(lambda r: r.url.endswith('/ui-action') and r.request.method == 'POST') as refused:
                    modal.locator('[data-owned-apply]').click()
                assert "secret_ref" in refused.value.json().get("error", ""), refused.value.json()
                expect(modal.locator('.owned-form-status')).to_contain_text("secret_ref")
                modal.locator('[data-block-config-field="secret_ref"]').fill("secret://workspace/telegram_bot")
                modal.locator('[data-block-config-field="allowed_chat_ids"]').fill("[123456789, -1001234567890]")
                modal.locator('[data-block-config-field="default_chat_id"]').fill("123456789")
                modal.locator('[data-block-config-field="parse_mode"]').select_option("HTML")
                receive.check()
                poll.fill("12")
                with page.expect_response(lambda r: r.url.endswith('/ui-action') and r.request.method == 'POST') as saved:
                    modal.locator('[data-owned-apply]').click()
                assert not saved.value.json().get("error"), saved.value.json()
                if modal.is_visible():
                    close_modal(modal)
                page.reload()
                expect(card.locator('.node-data-preview')).to_have_text("Send and receive · 2 chats")
                open_modal(card, modal)
                assert json.loads(modal.locator('[data-block-config-field="allowed_chat_ids"]').input_value()) == [123456789, -1001234567890]
                expect(modal.locator('[data-block-config-field="parse_mode"]')).to_have_value("HTML")
                expect(receive).to_be_checked()
                expect(poll).to_have_value("12")
                for width, height in VIEWPORTS:
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(100)
                    geometry(modal)
                    page.screenshot(path=artifact_path("telegram-" + origin + "-" + str(width) + ".png"))
                summary = modal.locator('.owned-advanced').first.locator('summary')
                summary.scroll_into_view_if_needed()
                summary.focus()
                summary.press('Enter')
                expect(modal.locator('.owned-advanced').first).to_have_attribute('open', '')
                expect(modal.locator('[data-block-config-field="max_updates_per_minute"]')).to_be_visible()
                geometry(modal)
                page.screenshot(path=artifact_path("telegram-" + origin + "-advanced-320.png"))
                page.set_viewport_size({"width": 1440, "height": 900})
                close_modal(modal)
                card.click(position={"x": 20, "y": 20})
                inspector = page.locator('[data-properties-surface="inspector"][data-node-id="ui-case"]:visible')
                expect(inspector).to_be_visible()
                assert inspector.evaluate('el => el.scrollWidth <= el.clientWidth+2')
                panel_receive = inspector.locator('[data-block-config-field="receive_updates"]')
                expect(panel_receive).to_be_checked()
                expect(inspector.locator('[data-block-config-field="poll_timeout_sec"]')).to_be_visible()
                panel_receive.uncheck()
                expect(inspector.locator('[data-block-config-field="poll_timeout_sec"]')).to_be_hidden()
                inspector.locator('[data-inspector-tab="ports"]').click()
                expect(inspector.locator('[data-inspector-panel-tab="ports"]')).to_be_visible()
                for port in ("message", "file", "received", "sent", "error"):
                    expect(inspector.locator('[data-inspector-panel-tab="ports"]')).to_contain_text(port)
                inspector.locator('[data-inspector-tab="general"]').click()
                expect(inspector.locator('[data-inspector-panel-tab="general"]')).to_be_visible()
                page.screenshot(path=artifact_path("telegram-" + origin + "-inspector.png"))
                page.goto(server.base_url + "/")
                page.locator("#homeApplicationSettingsButton").click()
                page.locator("#applicationLanguageSelect").select_option("fr")
                page.wait_for_function("window.CWMessages.getLanguage() === 'fr'")
                page.goto(url)
                # The card preview follows the interface language (the inspector draft above was not applied);
                # it also proves the French editor is ready before the modal is opened.
                expect(card.locator('.node-data-preview')).to_have_text("Envoi et réception · 2 discussions")
                open_modal(card, modal)
                expect(modal.locator('.owned-advanced').first.locator('summary')).to_have_text("Réglages avancés")
                expect(modal.locator('.owned-notice')).to_contain_text("Le jeton du bot reste dans le wallet")
                expect(modal.locator('label[for$="-enabled"]')).to_have_text("Activer l’accès distant")
                expect(modal.locator('[data-owned-apply]')).to_have_text("Appliquer")
                for width, height in ((1440, 900), (390, 740)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(100)
                    geometry(modal)
                    page.screenshot(path=artifact_path("telegram-" + origin + "-fr-" + str(width) + ".png"))
                assert not errors, errors
                assert page.evaluate("!window.CWBlockUiBlocks?.telegram")
            finally:
                browser.close()
        print("[ok] telegram " + origin + " UI", flush=True)


if __name__ == "__main__":
    main()
