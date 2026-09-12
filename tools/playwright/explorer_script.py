"""The Node/Playwright program the Exploration Agent drives.

Kept as a Python string so the platform ships as a single artifact. It is
written into ``<project>/.aiqa/explore.mjs`` at run time and executed with the
project's own Playwright installation, so it always matches the version the
team already uses.

It emits a single JSON document on stdout between sentinel markers, which keeps
parsing robust even when Playwright writes its own noise to the stream.
"""

BEGIN = "<<<AIQA_EXPLORE_BEGIN>>>"
END = "<<<AIQA_EXPLORE_END>>>"

EXPLORER_MJS = r"""
// AI QA Engineer — application exploration probe (generated; safe to delete).
import { chromium } from 'playwright';

const BEGIN = '<<<AIQA_EXPLORE_BEGIN>>>';
const END = '<<<AIQA_EXPLORE_END>>>';

const config = JSON.parse(process.argv[2] || '{}');
const baseUrl = config.baseUrl || 'http://localhost:3000';
const maxPages = config.maxPages ?? 5;
const timeout = config.timeout ?? 20000;
const startPaths = config.paths && config.paths.length ? config.paths : ['/'];
const screenshotDir = config.screenshotDir || null;

/** Collect every interactive element with a ranked locator recommendation. */
async function harvest(page) {
  return page.evaluate(() => {
    const SELECTABLE = 'input,select,textarea,button,a[href],[role="button"],[role="link"],[role="tab"],[role="checkbox"],[role="radio"],[contenteditable="true"]';

    const visible = (el) => {
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };

    const labelFor = (el) => {
      if (el.id) {
        const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (lab && lab.textContent) return lab.textContent.trim();
      }
      const parentLabel = el.closest('label');
      if (parentLabel && parentLabel.textContent) return parentLabel.textContent.trim();
      return el.getAttribute('aria-label') || '';
    };

    const roleOf = (el) => {
      const explicit = el.getAttribute('role');
      if (explicit) return explicit;
      const tag = el.tagName.toLowerCase();
      if (tag === 'a') return 'link';
      if (tag === 'button') return 'button';
      if (tag === 'select') return 'combobox';
      if (tag === 'textarea') return 'textbox';
      if (tag === 'input') {
        const type = (el.getAttribute('type') || 'text').toLowerCase();
        if (type === 'checkbox') return 'checkbox';
        if (type === 'radio') return 'radio';
        if (['submit', 'button', 'reset'].includes(type)) return 'button';
        return 'textbox';
      }
      return tag;
    };

    const elements = [];
    for (const el of document.querySelectorAll(SELECTABLE)) {
      if (!visible(el)) continue;

      const testId =
        el.getAttribute('data-testid') || el.getAttribute('data-test-id') ||
        el.getAttribute('data-test') || el.getAttribute('data-cy') || null;
      const label = labelFor(el);
      const placeholder = el.getAttribute('placeholder') || null;
      const text = (el.textContent || '').trim().slice(0, 80) || null;
      const role = roleOf(el);
      const accessibleName = label || text || placeholder || el.getAttribute('name') || '';

      // Ranked locator strategy — matches the org standard's priority order.
      let recommended = '', strategy = '', confidence = 0.3;
      const alternatives = [];
      if (testId) {
        recommended = `getByTestId('${testId}')`; strategy = 'getByTestId'; confidence = 0.98;
      } else if (accessibleName && role) {
        recommended = `getByRole('${role}', { name: '${accessibleName.replace(/'/g, "\\'")}' })`;
        strategy = 'getByRole'; confidence = 0.85;
      } else if (label) {
        recommended = `getByLabel('${label.replace(/'/g, "\\'")}')`; strategy = 'getByLabel'; confidence = 0.8;
      } else if (placeholder) {
        recommended = `getByPlaceholder('${placeholder.replace(/'/g, "\\'")}')`; strategy = 'getByPlaceholder'; confidence = 0.7;
      } else if (el.id) {
        recommended = `locator('#${el.id}')`; strategy = 'css'; confidence = 0.5;
      } else if (el.getAttribute('name')) {
        recommended = `locator('[name="${el.getAttribute('name')}"]')`; strategy = 'css'; confidence = 0.45;
      }
      if (el.id && strategy !== 'css') alternatives.push(`locator('#${el.id}')`);
      if (el.getAttribute('name')) alternatives.push(`locator('[name="${el.getAttribute('name')}"]')`);
      if (text && role === 'button') alternatives.push(`getByText('${text.replace(/'/g, "\\'")}')`);

      elements.push({
        role, name: accessibleName, tag: el.tagName.toLowerCase(),
        test_id: testId, label: label || null, placeholder, text,
        input_type: el.getAttribute('type'),
        required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
        recommended_locator: recommended, locator_strategy: strategy,
        confidence, alternatives: alternatives.slice(0, 3),
      });
    }

    const forms = Array.from(document.querySelectorAll('form')).map((form, i) => ({
      index: i,
      id: form.id || null,
      name: form.getAttribute('name') || null,
      action: form.getAttribute('action') || null,
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      field_count: form.querySelectorAll('input,select,textarea').length,
      submit_text: (form.querySelector('[type="submit"],button')?.textContent || '').trim() || null,
    }));

    const navigations = Array.from(document.querySelectorAll('a[href]'))
      .map((a) => a.getAttribute('href'))
      .filter((h) => h && !h.startsWith('#') && !h.startsWith('javascript:') && !h.startsWith('mailto:'))
      .slice(0, 60);

    return {
      title: document.title,
      elements,
      forms,
      navigations: Array.from(new Set(navigations)),
      dom_size: document.documentElement.outerHTML.length,
    };
  });
}

(async () => {
  const output = { base_url: baseUrl, snapshots: [], unreachable: [], errors: [] };
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    page.setDefaultTimeout(timeout);

    const queue = [...startPaths];
    const seen = new Set();

    while (queue.length && output.snapshots.length < maxPages) {
      const path = queue.shift();
      const url = path.startsWith('http') ? path : new URL(path, baseUrl).toString();
      if (seen.has(url)) continue;
      seen.add(url);

      try {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout });
        await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {});
        const data = await harvest(page);

        let screenshotPath = null;
        if (screenshotDir) {
          screenshotPath = `${screenshotDir}/page-${output.snapshots.length + 1}.png`;
          await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => { screenshotPath = null; });
        }

        output.snapshots.push({ url: page.url(), screenshot_path: screenshotPath, ...data });

        // Breadth-first: follow same-origin links we have not visited yet.
        for (const href of data.navigations) {
          try {
            const next = new URL(href, page.url());
            if (next.origin === new URL(baseUrl).origin && !seen.has(next.toString())) {
              queue.push(next.toString());
            }
          } catch { /* ignore malformed hrefs */ }
        }
      } catch (err) {
        output.unreachable.push(url);
        output.errors.push(`${url}: ${String(err).slice(0, 200)}`);
      }
    }
    await context.close();
  } catch (err) {
    output.errors.push(`launch failed: ${String(err).slice(0, 400)}`);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }

  process.stdout.write('\n' + BEGIN + '\n' + JSON.stringify(output) + '\n' + END + '\n');
})();
"""
