/**
 * Real Chrome, local only, no npm dependencies. Uses Chrome DevTools Protocol
 * over subprocess pipes so an unauthenticated debug TCP port is never opened.
 */
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {mkdir, mkdtemp, readFile, rm, writeFile} from "node:fs/promises";
import {performance} from "node:perf_hooks";
import {DatabaseSync} from "node:sqlite";
import {setTimeout as sleep} from "node:timers/promises";
import path from "node:path";

const [server, fixturePath, output, mode] = process.argv.slice(2);
const base = new URL(server);
assert.equal(base.protocol, "http:");
assert.equal(base.hostname, "127.0.0.1");
assert.match(fixturePath, /^\/private\/tmp\/ssl-nav-/);
assert.ok(["before", "after", "warm", "inspect"].includes(mode));
await mkdir(output, {recursive: true});
const profile = await mkdtemp("/private/tmp/ssl-nav-cdp.");
const binary = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const chrome = spawn(binary, [
    "--headless=new", "--remote-debugging-pipe", `--user-data-dir=${profile}`,
    "--disable-background-networking", "--disable-extensions", "--disable-sync",
    "--disable-crash-reporter", "--disable-breakpad", "--no-first-run",
    "--no-default-browser-check", "--no-proxy-server",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "about:blank",
], {stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"]});
const pending = new Map();
const collected = {mode, navigation: [], browserErrors: [], errors: []};
let seq = 0, input = "", stderr = "";
chrome.stderr.on("data", chunk => { stderr = (stderr + chunk).slice(-5000); });
chrome.stdio[4].setEncoding("utf8");
chrome.stdio[4].on("data", chunk => {
    input += chunk;
    for (let end = input.indexOf("\0"); end !== -1; end = input.indexOf("\0")) {
        const packet = JSON.parse(input.slice(0, end));
        input = input.slice(end + 1);
        if (packet.id && pending.has(packet.id)) {
            const wait = pending.get(packet.id);
            clearTimeout(wait.timeout);
            pending.delete(packet.id);
            if (packet.error) wait.reject(Error(JSON.stringify(packet.error)));
            else wait.resolve(packet.result);
        } else if (packet.method === "Runtime.exceptionThrown") {
            const {exceptionDetails} = packet.params;
            collected.browserErrors.push(exceptionDetails.exception?.description ?? exceptionDetails.text);
        }
    }
});
chrome.on("close", (code, signal) => {
    for (const wait of pending.values()) {
        clearTimeout(wait.timeout);
        wait.reject(Error(`Chrome exited (${code}, ${signal}): ${stderr}`));
    }
    pending.clear();
});
function send(method, params = {}, sessionId) {
    const id = ++seq;
    return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
            pending.delete(id);
            reject(Error(`Chrome command timed out: ${method}\n${stderr}`));
        }, 15000);
        pending.set(id, {resolve, reject, timeout});
        chrome.stdio[3].write(JSON.stringify({id, method, params, sessionId}) + "\0");
    });
}

async function newPage({width = 1440, height = 1000, mobile = false} = {}) {
    const {browserContextId} = await send("Target.createBrowserContext", {disposeOnDetach: true});
    const {targetId} = await send("Target.createTarget", {url: "about:blank", browserContextId});
    const {sessionId} = await send("Target.attachToTarget", {targetId, flatten: true});
    const call = (method, args) => send(method, args, sessionId);
    await call("Page.enable");
    await call("Runtime.enable");
    await call("Emulation.setDeviceMetricsOverride", {
        width, height, deviceScaleFactor: 1, mobile, screenWidth: width, screenHeight: height,
    });
    if (mobile) await call("Emulation.setTouchEmulationEnabled", {enabled: true, maxTouchPoints: 1});
    const evaluate = async (expression) => {
        const {result, exceptionDetails} = await call("Runtime.evaluate", {
            expression, returnByValue: true, awaitPromise: true,
        });
        if (exceptionDetails) throw Error(exceptionDetails.exception?.description ?? exceptionDetails.text);
        return result.value;
    };
    const mainText = () => evaluate("document.body?.innerText ?? ''");
    const htmlError = () => evaluate("document.querySelector('[data-testid=stException]')?.innerText ?? ''");
    async function until(expression, description, limit = 18000) {
        const deadline = performance.now() + limit;
        while (performance.now() < deadline) {
            const result = await evaluate(expression);
            if (result) return result;
            const exception = await htmlError();
            if (exception) throw Error(`Streamlit exception: ${exception}`);
            await sleep(65);
        }
        throw Error(`Timed out waiting for ${description}.\n${(await mainText()).slice(0, 2300)}`);
    }
    const textInBody = (value) => until(
        `(document.body?.innerText ?? '').includes(${JSON.stringify(value)})`,
        JSON.stringify(value),
    );
    const matchText = (value, selector = "button", contains = false) => `
        [...document.querySelectorAll(${JSON.stringify(selector)})].find(el => {
            const rendered = el.innerText?.replace(/\\s+/g," ").trim();
            return !!el.getClientRects().length && ${contains ? `rendered?.includes(${JSON.stringify(value)})` :
                `rendered === ${JSON.stringify(value)}`};
        })
    `;
    async function click(expression) {
        const point = await until(`(() => {
            const el = ${expression};
            if (!el || !el.getClientRects().length || el.disabled) return false;
            el.scrollIntoView({block:"center",inline:"nearest"});
            const r = el.getBoundingClientRect();
            const x = r.left + r.width / 2, y = r.top + r.height / 2;
            return el.contains(document.elementFromPoint(x,y)) ? [x,y] : false;
        })()`, expression);
        const [x, y] = point;
        await call("Input.dispatchMouseEvent", {type: "mouseMoved", x, y});
        await call("Input.dispatchMouseEvent", {type: "mousePressed", x, y, button: "left", clickCount: 1});
        await call("Input.dispatchMouseEvent", {type: "mouseReleased", x, y, button: "left", clickCount: 1});
    }
    const clickCSS = (value) => click(`document.querySelector(${JSON.stringify(value)})`);
    const clickText = (value, selector, contains = false) => click(matchText(value, selector, contains));
    const link = (route) => `[...document.querySelectorAll('a[href]')].find(a =>
        a.getClientRects().length && new URL(a.href).origin === location.origin
        && new URL(a.href).pathname === ${JSON.stringify(route)})`;
    const go = (route) => call("Page.navigate", {url: new URL(route, base).href});
    const routeIs = (route) => until(
        `location.pathname === ${JSON.stringify(route)}`, "route " + route);
    const key = async (value, code, number) => {
        const args = {key: value, code, windowsVirtualKeyCode: number, nativeVirtualKeyCode: number};
        await call("Input.dispatchKeyEvent", {type: "rawKeyDown", ...args});
        await call("Input.dispatchKeyEvent", {type: "keyUp", ...args});
    };
    const login = async (owner = "qa_private_a") => {
        await clickCSS('input[autocomplete="username"]');
        await call("Input.insertText", {text: owner});
        await clickCSS('input[autocomplete="current-password"]');
        await call("Input.insertText", {text: "synthetic-navigation-only"});
        await clickText("Entrar");
        await until("!document.querySelector('input[autocomplete=username]')", "login accepted");
    };
    const screenshot = async (name) => {
        await evaluate("scrollTo(0,0)");
        await evaluate("document.fonts.ready.then(() => true)");
        const {data} = await call("Page.captureScreenshot", {format: "png", fromSurface: true});
        await writeFile(path.join(output, name + ".png"), Buffer.from(data, "base64"));
    };
    const historyStep = async (offset) => {
        const {entries, currentIndex} = await call("Page.getNavigationHistory");
        const entry = entries[currentIndex + offset];
        assert.ok(entry, "Chrome must have a real history entry");
        await call("Page.navigateToHistoryEntry", {entryId: entry.id});
        return entry.url;
    };
    const close = () => send("Target.disposeBrowserContext", {browserContextId});
    return {call, evaluate, mainText, until, textInBody, click, clickCSS, clickText, link, go,
        routeIs, key, login, screenshot, historyStep, close};
}

async function events() {
    const content = await readFile(path.join(fixturePath, "events.jsonl"), "utf8");
    return content.trim().split("\n").filter(Boolean).map(line => JSON.parse(line));
}
async function timed(label, callback) {
    const started = performance.now();
    await callback();
    collected.navigation.push({label, ms: Math.round(performance.now() - started)});
}
async function beforeRadar(page) {
    await page.until(
        "document.querySelector('[data-testid=stApp]')?.dataset.testScriptState === 'notRunning'",
        "legacy page fully rendered",
    );
    await page.clickText("Abrir herramientas y resultados", "summary", true);
    await page.clickCSS('[data-testid=stExpander] details[open] [role=combobox]');
    await page.clickText("Analizar", "[role=option]");
    await sleep(500);
    await page.clickText("Abrir sección");
    await page.clickText("Mi radar", "button", true);
    await page.textInBody("Todas mis empresas");
}

try {
    const desktop = await newPage();
    await desktop.go("/");
    await timed("cold login to Hoy", async () => {
        await desktop.login();
        await desktop.textInBody("Hoy · cartera y posibles entradas");
    });
    await desktop.screenshot(`${mode}-desktop-hoy`);
    if (mode === "inspect") {
        collected.inspection = await desktop.evaluate(`({
            text: document.body.innerText.slice(0,3500),
            controls: [...document.querySelectorAll('button,a[href],summary')].slice(0,55)
                .map(el => [el.tagName,el.innerText,el.getAttribute('href')])
        })`);
    } else if (mode === "before") {
        await timed("Hoy to Radar through legacy menus", () => beforeRadar(desktop));
        await desktop.screenshot("before-desktop-radar");
        const mobile = await newPage({width: 390, height: 844, mobile: true});
        await mobile.go("/");
        await mobile.login();
        await mobile.textInBody("Hoy · cartera y posibles entradas");
        await mobile.screenshot("before-mobile-hoy");
        await timed("Mobile Hoy to Radar through legacy menus", () => beforeRadar(mobile));
        await mobile.screenshot("before-mobile-radar");
        await mobile.close();
    } else if (mode === "warm") {
        await runWarm(desktop);
    } else {
        await runAfter(desktop);
    }
    collected.providerCalls = (await events()).filter(event => event.event === "provider");
    collected.blockedExternal = (await events()).filter(event => event.event === "blocked_external").length;
    assert.equal(collected.blockedExternal, 0);
    assert.deepEqual(collected.browserErrors, []);
    if (mode === "before") assert.equal(collected.providerCalls.length, 0);
    await desktop.close();
} catch (error) {
    collected.errors.push(error.stack);
    process.exitCode = 1;
} finally {
    await writeFile(path.join(output, mode + "-chrome.json"), JSON.stringify(collected, null, 2));
    console.log(JSON.stringify(collected, null, 2));
    chrome.kill("SIGTERM");
    chrome.stdio[3].end();
    chrome.stdio[4].destroy();
    chrome.stderr.destroy();
    await sleep(250);
    await rm(profile, {recursive: true, force: true});
}

function checkpoint() {
    const db = new DatabaseSync(path.join(fixturePath, "qa_private_a.sqlite"), {readOnly: true});
    try {
        db.exec("PRAGMA busy_timeout = 1000");
        const row = db.prepare("SELECT checkpoint FROM radar_review_checkpoints WHERE owner=?").get("qa_private_a");
        return row ? JSON.parse(row.checkpoint) : null;
    } finally { db.close(); }
}

async function checkpointUntil(predicate, label, timeout = 12000) {
    const deadline = performance.now() + timeout;
    while (performance.now() < deadline) {
        try {
            const value = checkpoint();
            if (value && predicate(value)) return value;
        } catch (error) {
            if (!/database is locked|database table is locked/.test(String(error))) throw error;
        }
        await sleep(50);
    }
    throw Error(`No durable Radar checkpoint for ${label}`);
}

async function runWarm(desktop) {
    await desktop.until(
        "document.querySelector('[data-testid=stApp]')?.dataset.testScriptState === 'notRunning'",
        "legacy page fully rendered",
    );
    await desktop.clickText("Abrir Radar de la versión anterior (prueba)");
    await desktop.textInBody("Todas mis empresas");
    await desktop.screenshot("warm-radar-before-switch");
    await desktop.clickText("Aplicar actualización de prueba");
    await desktop.textInBody("Hoy · cartera y posibles entradas");
    assert.equal(await desktop.evaluate("!!document.querySelector('input[autocomplete=username]')"), false);
    await desktop.click(desktop.link("/radar"));
    await desktop.routeIs("/radar");
    await desktop.textInBody("Todas mis empresas");
    await desktop.clickText("Actualizar ahora todas mis favoritas", "button", true);
    const stored = await checkpointUntil(row => Object.keys(row.outcomes).length >= 12, "two warm batches", 30000);
    await desktop.click(desktop.link("/favoritas"));
    await desktop.routeIs("/favoritas");
    await desktop.textInBody("Mis listas");
    await checkpointUntil(row => !row.worker, "warm pause");
    assert.ok(stored.outcomes.QAA000);
    assert.equal(stored.outcomes.QAA002.status, "no_data");
    collected.warm = {
        preservedAuthentication: true,
        saved: Object.keys(stored.outcomes).length,
        expectedFailureRecorded: true,
    };
    await desktop.until(
        "document.querySelector('[data-testid=stApp]')?.dataset.testScriptState === 'notRunning'",
        "favorites navigation settled after interrupting Radar",
    );
    await desktop.click(desktop.link("/radar"));
    await desktop.routeIs("/radar");
    await desktop.textInBody("Reanudar pendientes");
    await desktop.screenshot("warm-radar-after-switch");
}

async function runAfter(desktop) {
    // The same synthetic test database is inspected read-only for resumability.
    // Browser steps are defined independently of the app's implementation.
    const navigation = [
        ["/cartera", "Mi cartera"],
        ["/radar", "Todas mis empresas"],
        ["/favoritas", "Mis listas"],
        ["/mas", "Resultados, métodos y ajustes"],
    ];
    for (const [route, ready] of navigation) {
        await timed("Desktop " + route, async () => {
            await desktop.click(desktop.link(route));
            await desktop.routeIs(route);
            await desktop.textInBody(ready);
        });
    }
    await desktop.historyStep(-1);
    await desktop.routeIs("/favoritas");
    await desktop.textInBody("Mis listas");
    await desktop.historyStep(1);
    await desktop.routeIs("/mas");
    await desktop.textInBody("Resultados, métodos y ajustes");
    collected.history = "Chrome back/forward restored both path and visible view";

    // Move focus with an actual Tab key and activate via Enter. Pre-focus the
    // preceding link so the assertion tests the browser's tab order and target.
    await desktop.evaluate(`${desktop.link("/cartera")}.focus()`);
    await desktop.key("Tab", "Tab", 9);
    assert.equal(await desktop.evaluate("new URL(document.activeElement.href).pathname"), "/radar");
    await desktop.key("Enter", "Enter", 13);
    await desktop.routeIs("/radar");
    await desktop.textInBody("Todas mis empresas");
    collected.keyboard = "Tab advanced Cartera → Radar; Enter opened Radar";
    await desktop.screenshot("after-desktop-radar");
    await desktop.clickText("Hoy", ".st-key-main_navigation_container a");
    await desktop.routeIs("/");
    await desktop.textInBody("Hoy · cartera y posibles entradas");

    await desktop.go("/empresa?ticker=QAA000&release=qa");
    await desktop.routeIs("/empresa");
    if (await desktop.evaluate("!!document.querySelector('input[autocomplete=username]')")) {
        await desktop.login();
    } else {
        await desktop.until(
            "!!document.querySelector('input[autocomplete=username]') || document.body?.innerText.includes('QAA000')",
            "company or authentication",
        );
        if (await desktop.evaluate("!!document.querySelector('input[autocomplete=username]')")) await desktop.login();
    }
    await desktop.textInBody("QAA000");
    assert.equal(await desktop.evaluate("new URLSearchParams(location.search).get('ticker')"), "QAA000");
    assert.equal(await desktop.evaluate("new URLSearchParams(location.search).get('release')"), "qa");
    collected.company = "Private company direct URL restored without provider fetch";
    await desktop.screenshot("after-desktop-company");

    const mobile = await newPage({width: 390, height: 844, mobile: true});
    await mobile.go("/");
    await mobile.login();
    await mobile.textInBody("Hoy · cartera y posibles entradas");
    await mobile.screenshot("after-mobile-hoy");
    await timed("Mobile direct Radar", async () => {
        await mobile.click(mobile.link("/radar"));
        await mobile.routeIs("/radar");
        await mobile.textInBody("Todas mis empresas");
    });
    await mobile.screenshot("after-mobile-radar");
    collected.mobile = await mobile.evaluate(`({
        width: innerWidth,
        horizontalOverflow: Math.max(0, document.documentElement.scrollWidth-innerWidth),
        nav: [...document.querySelectorAll('.st-key-main_navigation_container a[href]')]
            .filter(a => a.getClientRects().length).map(a => [a.innerText,new URL(a.href).pathname,Math.round(a.getBoundingClientRect().height)]),
    })`);
    assert.equal(collected.mobile.horizontalOverflow, 0);
    assert.deepEqual(collected.mobile.nav.map(row => row[0]), ["Hoy", "Cartera", "Radar", "Favoritas", "Más"]);
    assert.ok(collected.mobile.nav.every(row => row[2] >= 44));
    await mobile.click(mobile.link("/empresa"));
    await mobile.routeIs("/empresa");
    await mobile.textInBody("Entender una empresa");
    await mobile.click(mobile.link("/favoritas"));
    await mobile.textInBody("Mis listas");
    await mobile.click(mobile.link("/cartera"));
    await mobile.textInBody("Mi cartera");
    await mobile.clickText("Cuenta", "button", true);
    await mobile.textInBody("Cerrar sesión");
    assert.equal(await mobile.evaluate(
        "!!document.querySelector('[data-testid=stSidebar]')?.getClientRects().length",
    ), false);
    await mobile.key("Escape", "Escape", 27);
    await mobile.click(mobile.link("/radar"));
    await mobile.textInBody("Todas mis empresas");

    const other = await newPage();
    await other.go("/favoritas");
    await other.login("qa_private_b");
    await other.routeIs("/favoritas");
    await other.textInBody("Mis listas");
    assert.equal(await other.evaluate("document.body.innerText.includes('QAA000')"), false);
    // Streamlit's virtualized dataframe cells are canvas, not DOM text.
    // Count and scoped search verify the visible private list.
    await other.textInBody("2 de 300 empresas guardadas");
    await other.clickText("Grupo · 0", "[role=tab]");
    await other.textInBody("Todavía no hay empresas en esta lista");
    await other.clickText("Mi lista · 2", "[role=tab]");
    const filter = "input[aria-label='Buscar dentro de esta lista']";
    await other.clickCSS(filter);
    await other.call("Input.insertText", {text: "QAA000"});
    await other.key("Enter", "Enter", 13);
    await other.textInBody("Ninguna empresa coincide");
    await other.clickText("Cuenta", "button", true);
    await other.textInBody("Sesión iniciada como Cuenta sintética Beta");
    const logoutCount = await other.evaluate(`[...document.querySelectorAll("button")]
        .filter(button => button.getClientRects().length && button.innerText.trim() === "Cerrar sesión").length`);
    assert.equal(logoutCount, 1, "Account menu must be the only logout control");
    await other.clickText("Cerrar sesión");
    await other.until("!!document.querySelector('input[autocomplete=username]')", "signed out");
    const fixture = JSON.parse(await readFile(path.join(fixturePath, "fixture.json"), "utf8"));
    await other.go("/?digest=" + fixture.report_ids.qa_private_a);
    await other.until("!!document.querySelector('input[autocomplete=username]') || document.body.innerText.includes('Volver a Hoy')", "archive or authentication");
    if (await other.evaluate("!!document.querySelector('input[autocomplete=username]')")) {
        await other.login("qa_private_b");
    }
    await other.textInBody("Volver a Hoy");
    assert.equal(await other.evaluate("document.body.innerText.includes('QAA000')"), false);
    collected.isolation = "Second account did not inherit favorites or archived report from first";
    collected.passiveProviderCalls = (await events()).filter(event => event.event === "provider").length;
    assert.equal(collected.passiveProviderCalls, 0,
        "Navigation, direct company, mobile and account switches must not call providers");

    const startedReview = performance.now();
    await mobile.clickText("Actualizar ahora todas mis favoritas", "button", true);
    const initial = await checkpointUntil(row => Object.keys(row.outcomes).length >= 6, "first saved batch");
    const firstBlockMs = Math.round(performance.now() - startedReview);
    await mobile.click(mobile.link("/favoritas"));
    await mobile.routeIs("/favoritas");
    await mobile.textInBody("Mis listas");
    const paused = await checkpointUntil(row => !row.worker && Object.keys(row.outcomes).length < row.tickers.length, "paused off Radar");
    const beforeResume = Object.keys(paused.outcomes).length;
    assert.ok(beforeResume >= Object.keys(initial.outcomes).length);
    assert.equal(paused.tickers.length, 142);
    await mobile.go("/radar");
    await mobile.until("!!document.querySelector('input[autocomplete=username]') || document.body.innerText.includes('Revisión pausada')", "resume or authentication");
    if (await mobile.evaluate("!!document.querySelector('input[autocomplete=username]')")) await mobile.login();
    await mobile.textInBody("Revisión pausada");
    await mobile.screenshot("after-mobile-radar-paused");
    const startedResume = performance.now();
    await mobile.clickText("Reanudar pendientes", "button", true);
    const finished = await checkpointUntil(row => !row.worker && Object.keys(row.outcomes).length === 142, "all 142 favorites", 90000);
    assert.equal(finished.run_id, initial.run_id);
    assert.equal(finished.outcomes.QAA002.status, "no_data");
    assert.equal(finished.outcomes.QAA003.status, "stale");
    const providers = (await events()).filter(event => event.event === "provider");
    const assetCalls = providers.filter(event => event.kind === "price" && /^QAA/.test(event.symbol));
    const repeatedCompleted = Object.keys(paused.outcomes)
        .filter(symbol => assetCalls.filter(event => event.symbol === symbol).length !== 1);
    assert.deepEqual(repeatedCompleted, [], "Resume must not fetch the committed block again");
    collected.radar = {beforeResume, firstBlockMs, resumeMs: Math.round(performance.now() - startedResume),
        total: finished.tickers.length, statusCounts: Object.fromEntries(
        [...new Set(Object.values(finished.outcomes).map(item => item.status))]
            .map(status => [status, Object.values(finished.outcomes).filter(item => item.status === status).length]),
    )};
    await mobile.textInBody("Revisión terminada");
    await mobile.screenshot("after-mobile-radar-finished");
    const companyBefore = (await events()).length;
    await desktop.clickText("Actualizar datos de QAA000", "button", true);
    let companyEvents = [];
    const deadline = performance.now() + 10000;
    while (performance.now() < deadline) {
        companyEvents = (await events()).slice(companyBefore);
        if (companyEvents.some(event => event.event === "main_end")
                && companyEvents.some(event => event.kind === "price")) break;
        await sleep(65);
    }
    await desktop.textInBody("Vela utilizada:");
    const companyCalls = companyEvents
        .filter(event => event.kind === "price" && /^QAA/.test(event.symbol));
    assert.deepEqual(companyCalls.map(event => event.symbol), ["QAA000"]);
    collected.explicitCompanyRefresh = "One asset only; context benchmarks are separate";
    await mobile.close();
    await other.close();
}
