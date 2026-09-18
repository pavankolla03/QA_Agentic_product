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
// QAgentic — application exploration probe (generated; safe to delete).
import { chromium } from 'playwright';

const BEGIN = '<<<AIQA_EXPLORE_BEGIN>>>';
const END = '<<<AIQA_EXPLORE_END>>>';

const config = JSON.parse(process.argv[2] || '{}');
const baseUrl = config.baseUrl || 'http://localhost:3000';
const maxPages = config.maxPages ?? 5;
const timeout = config.timeout ?? 20000;
const startPaths = config.paths && config.paths.length ? config.paths : ['/'];
const screenshotDir = config.screenshotDir || null;
const credentials = config.credentials || null;

// Links that end the session. Following one mid-crawl logs the crawler out, and
// every page after it is the login screen again — which looks exactly like an
// application with one page, and is how a crawl silently loses everything it
// had not reached yet.
const SESSION_ENDING = /(^|[\/_-])(logout|log-out|signout|sign-out|disconnect)([\/_-]|$|\?)/i;

/** The one place that writes the result, so the sentinels cannot drift apart. */
function emit(output) {
  process.stdout.write('\n' + BEGIN + '\n' + JSON.stringify(output) + '\n' + END + '\n');
}

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

/**
 * Sign in, and be certain about whether it worked.
 *
 * The verification is the whole point. A crawler that submits a form and
 * assumes success walks away with a session it does not have, follows every
 * navigation link, is redirected to the login page each time, and reports a
 * five-page application that is really one page five times. Everything built
 * from that crawl is invention, because there was nothing else to build from.
 *
 * Returns true only when a password field was filled, submitted, and is gone.
 */
async function signIn(page, output) {
  if (!credentials || !credentials.username || !credentials.password) return true;

  const loginUrl = new URL(credentials.loginPath || '/', baseUrl).toString();
  try {
    await page.goto(loginUrl, { waitUntil: 'domcontentloaded', timeout });
  } catch (err) {
    output.errors.push('sign-in: could not open ' + loginUrl + ': ' + String(err).slice(0, 160));
    return false;
  }

  const password = page.locator('input[type="password"]').first();
  if (!(await password.count())) {
    output.errors.push('sign-in: no password field at ' + loginUrl + ' — give a login path if it is elsewhere');
    return false;
  }

  // Record the sign-in page before leaving it. It is the one screen every user
  // of the application meets, and signing in then crawling onwards left it out
  // of the map entirely — so the most important flow in the product had no
  // coverage, for the same reason it had no page: we walked straight past it.
  try {
    const data = await harvest(page);
    output.snapshots.push({ url: page.url(), status: 200, screenshot_path: null, ...data });
  } catch { /* the sign-in itself matters more than capturing the form */ }

  // The identifier is whichever text-ish input comes before the password. That
  // is what "the username field" means on every sign-in form, and it avoids
  // guessing from names that differ per application.
  const identifier = page.locator(
    'input[type="text"], input[type="email"], input[type="tel"], input:not([type])'
  ).first();
  try {
    if (await identifier.count()) await identifier.fill(credentials.username);
    await password.fill(credentials.password);
    for (const [name, value] of Object.entries(credentials.extra || {})) {
      const field = page.locator(`[name="${name}"], #${name}`).first();
      if (await field.count()) await field.fill(String(value));
    }
  } catch (err) {
    output.errors.push('sign-in: could not fill the form: ' + String(err).slice(0, 160));
    return false;
  }

  const submit = page.locator(
    'button[type="submit"], input[type="submit"], form button'
  ).first();
  try {
    if (await submit.count()) {
      await submit.click();
    } else {
      await password.press('Enter');
    }
    await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {});
  } catch (err) {
    output.errors.push('sign-in: could not submit: ' + String(err).slice(0, 160));
    return false;
  }

  // Still looking at a password field means the credentials were refused, the
  // form wanted something more, or the submit did nothing. Any of those is a
  // failure to sign in, and saying so is the only useful answer.
  if (await page.locator('input[type="password"]').count()) {
    const message = await page
      .locator('[role="alert"], .error, [data-testid*="error"]')
      .first()
      .textContent()
      .catch(() => null);
    output.errors.push(
      'sign-in failed: still on a page with a password field' +
        (message ? ' (' + message.trim().slice(0, 120) + ')' : '')
    );
    return false;
  }

  output.signed_in = true;
  output.landing_url = page.url();
  return true;
}

/** The three rungs, tried in order. Returns null when no browser starts. */
async function launchBrowser(output) {
  const attempts = [
    { label: 'bundled chromium', options: { headless: true } },
    { label: 'system chrome', options: { headless: true, channel: 'chrome' } },
    { label: 'system edge', options: { headless: true, channel: 'msedge' } },
  ];
  for (const attempt of attempts) {
    try {
      const browser = await chromium.launch(attempt.options);
      output.browser = attempt.label;
      return browser;
    } catch (err) {
      output.errors.push(attempt.label + ' unavailable: ' + String(err).split('\n')[0].slice(0, 200));
    }
  }
  return null;
}

/** Last resort: read the HTML over HTTP. No JavaScript runs, and it says so. */
async function harvestStatic(url) {
  const response = await fetch(url, { redirect: 'follow' });
  if (!response.ok) throw new Error('HTTP ' + response.status);
  const html = await response.text();

  const esc = (s) => String(s).replace(/'/g, "\'");
  const attr = (tag, name) => {
    const m = tag.match(new RegExp(name + '\s*=\s*["\']([^"\']*)["\']', 'i'));
    return m ? m[1] : null;
  };
  const elements = [];
  const tagRe = /<(input|select|textarea|button|a)\b([^>]*)>(?:([\s\S]*?)<\/\1>)?/gi;
  let match;
  while ((match = tagRe.exec(html)) && elements.length < 120) {
    const tagName = match[1].toLowerCase();
    const tag = '<' + tagName + ' ' + match[2] + '>';
    const inner = match[3];
    const type = (attr(tag, 'type') || '').toLowerCase();
    const testId = attr(tag, 'data-testid') || attr(tag, 'data-test-id') ||
                   attr(tag, 'data-test') || attr(tag, 'data-cy');
    const text = (inner || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 80) || null;
    const name = attr(tag, 'name');
    const id = attr(tag, 'id');
    const placeholder = attr(tag, 'placeholder');
    const label = attr(tag, 'aria-label');
    let role = tagName;
    if (role === 'a') role = 'link';
    else if (role === 'select') role = 'combobox';
    else if (role === 'textarea') role = 'textbox';
    else if (role === 'input') {
      role = ['submit', 'button', 'reset'].includes(type) ? 'button'
           : type === 'checkbox' ? 'checkbox' : type === 'radio' ? 'radio' : 'textbox';
    }
    const accessibleName = label || text || placeholder || name || '';
    if (!testId && !accessibleName && !id) continue;

    // Same ranked strategy as the browser path, and the same field names --
    // a snapshot that parses differently depending on how it was captured
    // would break every consumer downstream.
    let recommended = '', strategy = '', confidence = 0.3;
    const alternatives = [];
    if (testId) {
      recommended = "getByTestId('" + testId + "')"; strategy = 'getByTestId'; confidence = 0.98;
    } else if (accessibleName && role) {
      recommended = "getByRole('" + role + "', { name: '" + esc(accessibleName) + "' })";
      strategy = 'getByRole'; confidence = 0.85;
    } else if (label) {
      recommended = "getByLabel('" + esc(label) + "')"; strategy = 'getByLabel'; confidence = 0.8;
    } else if (placeholder) {
      recommended = "getByPlaceholder('" + esc(placeholder) + "')"; strategy = 'getByPlaceholder'; confidence = 0.7;
    } else if (id) {
      recommended = "locator('#" + id + "')"; strategy = 'css'; confidence = 0.5;
    } else if (name) {
      recommended = "locator('[name=\"" + name + "\"]')"; strategy = 'css'; confidence = 0.45;
    }
    if (id && strategy !== 'css') alternatives.push("locator('#" + id + "')");
    if (name) alternatives.push("locator('[name=\"" + name + "\"]')");
    if (text && role === 'button') alternatives.push("getByText('" + esc(text) + "')");

    elements.push({
      role, name: accessibleName, tag: tagName,
      test_id: testId, label: label || null, placeholder, text,
      input_type: type || null,
      required: /\brequired\b/i.test(match[2]) || /aria-required\s*=\s*["']true["']/i.test(match[2]),
      recommended_locator: recommended, locator_strategy: strategy,
      // Nothing was rendered, so nothing was seen. Cap the confidence below the
      // trust floor: these are candidates to verify, not verified locators.
      confidence: Math.min(confidence, 0.5),
      alternatives: alternatives.slice(0, 3),
    });
  }
  const navigations = [];
  const hrefRe = /<a\b[^>]*href\s*=\s*["']([^"'#][^"']*)["']/gi;
  let hrefMatch;
  while ((hrefMatch = hrefRe.exec(html)) && navigations.length < 60) {
    const href = hrefMatch[1];
    if (!href.startsWith('javascript:') && !href.startsWith('mailto:')) navigations.push(href);
  }
  const titleMatch = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  return {
    title: titleMatch ? titleMatch[1].trim() : '',
    elements, forms: [],
    navigations: Array.from(new Set(navigations)),
    dom_size: html.length,
  };
}

/** Crawl breadth-first over plain HTTP when no browser will start. */
async function crawlStatic(output) {
  const queue = [...startPaths];
  const seen = new Set();
  while (queue.length && output.snapshots.length < maxPages) {
    const path = queue.shift();
    const url = path.startsWith('http') ? path : new URL(path, baseUrl).toString();
    if (seen.has(url)) continue;
    seen.add(url);
    try {
      const data = await harvestStatic(url);
      output.snapshots.push({ url, status: 200, screenshot_path: null, ...data });
      for (const href of data.navigations) {
        try {
          const next = new URL(href, url);
          if (next.origin === new URL(baseUrl).origin && !seen.has(next.toString())) {
            queue.push(next.toString());
          }
        } catch { /* ignore malformed hrefs */ }
      }
    } catch (err) {
      output.unreachable.push(url);
      output.errors.push(url + ': ' + String(err).slice(0, 200));
    }
  }
}

(async () => {
  const output = { base_url: baseUrl, snapshots: [], unreachable: [], errors: [], simulated: false, browser: '', signed_in: false, landing_url: '' };
  let browser;
  try {
    browser = await launchBrowser(output);
    if (!browser) {
      // Every browser refused. Read the HTML instead and mark the whole crawl
      // simulated, so no caller mistakes this for a verified locator set.
      output.simulated = true;
      output.browser = 'http (no browser)';
      await crawlStatic(output);
      emit(output);
      return;
    }
    const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    page.setDefaultTimeout(timeout);

    const authenticated = await signIn(page, output);
    if (credentials && credentials.username && !authenticated) {
      // Credentials were offered and did not work. Crawling on would collect
      // the login page over and over and present it as the application, so the
      // honest move is to stop and say why.
      await context.close();
      await browser.close().catch(() => {});
      emit(output);
      return;
    }

    // Once signed in, the interesting pages hang off wherever the login landed,
    // not off the login page the caller named.
    const queue = output.landing_url ? [output.landing_url, ...startPaths] : [...startPaths];
    const seen = new Set();

    while (queue.length && output.snapshots.length < maxPages) {
      const path = queue.shift();
      const url = path.startsWith('http') ? path : new URL(path, baseUrl).toString();
      if (seen.has(url)) continue;
      seen.add(url);

      try {
        const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout });
        const status = response ? response.status() : 0;
        if (status >= 400) {
          // A 404 still renders a page, and a crawler that only checks for an
          // exception records the error page as a route of the application.
          output.unreachable.push(url);
          output.errors.push(url + ': HTTP ' + status);
          continue;
        }
        await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {});

        // Where we landed counts as visited too. Signing in redirects "/" to
        // the dashboard, so without this the dashboard is crawled twice and one
        // page of the budget buys nothing.
        if (page.url() !== url) {
          if (seen.has(page.url())) continue;
          seen.add(page.url());
        }

        const data = await harvest(page);

        let screenshotPath = null;
        if (screenshotDir) {
          screenshotPath = `${screenshotDir}/page-${output.snapshots.length + 1}.png`;
          await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => { screenshotPath = null; });
        }

        output.snapshots.push({ url: page.url(), status, screenshot_path: screenshotPath, ...data });

        // Breadth-first: follow same-origin links we have not visited yet.
        for (const href of data.navigations) {
          try {
            const next = new URL(href, page.url());
            if (SESSION_ENDING.test(next.pathname)) continue;   // never log ourselves out
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
    output.errors.push(`crawl failed: ${String(err).slice(0, 400)}`);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }

  emit(output);
})();
"""
