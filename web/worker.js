/* Pyodide Web Worker — UI 스레드를 막지 않기 위해 분리. */

importScripts("https://cdn.jsdelivr.net/pyodide/v0.26.2/full/pyodide.js");

let pyodide = null;
const PY_FILES = [
  "parse_input.py",
  "schedule.py",
  "review.py",
  "render_bracket.py",
  "build_template.py",
  "run.py",
];

async function setup() {
  postMessage({ type: "log", msg: "Pyodide 로딩 중 (최초 1회, 약 10MB)…" });
  pyodide = await loadPyodide({
    indexURL: "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/",
  });

  postMessage({ type: "log", msg: "openpyxl 설치 중…" });
  await pyodide.loadPackage("micropip");
  await pyodide.runPythonAsync(`
import micropip
await micropip.install('openpyxl')
  `);

  postMessage({ type: "log", msg: "Python 모듈 로드 중…" });
  pyodide.FS.mkdirTree("/home/pyodide/app");
  for (const f of PY_FILES) {
    const text = await (await fetch(`./py/${f}?v=1`)).text();
    pyodide.FS.writeFile(`/home/pyodide/app/${f}`, text);
  }
  pyodide.runPython(`
import sys
if '/home/pyodide/app' not in sys.path:
    sys.path.insert(0, '/home/pyodide/app')
import run
  `);

  postMessage({ type: "ready" });
}

self.onmessage = async (e) => {
  const { type, payload } = e.data;

  if (type === "init") {
    try {
      await setup();
    } catch (err) {
      postMessage({ type: "error", msg: String(err) + "\n" + (err.stack || "") });
    }
    return;
  }

  if (type === "template") {
    if (!pyodide) {
      postMessage({ type: "error", msg: "Pyodide가 아직 준비되지 않았습니다." });
      return;
    }
    try {
      const { prefill } = payload || {};
      postMessage({ type: "log", msg: "빈 양식 생성 중…" });
      const callTpl = pyodide.runPython(`
def _call_tpl(prefill):
    import run
    return run.build_empty_template_bytes(prefill=prefill)
_call_tpl
      `);
      const py_bytes = callTpl(prefill || "");
      const u8 = py_bytes.toJs();
      py_bytes.destroy();
      postMessage({ type: "template_done", payload: { xlsx: u8 } }, [u8.buffer]);
    } catch (err) {
      postMessage({ type: "error", msg: String(err) + "\n" + (err.stack || "") });
    }
    return;
  }

  if (type === "generate") {
    if (!pyodide) {
      postMessage({ type: "error", msg: "Pyodide가 아직 준비되지 않았습니다." });
      return;
    }
    try {
      const { bytes, dateStr, seed, iters, title } = payload;
      postMessage({ type: "log", msg: `대진 생성 중 (시드 ${seed}, 반복 ${iters})…` });

      const callGen = pyodide.runPython(`
def _call(input_buf, date_str, seed, iters, title):
    import run
    return run.generate_bracket(bytes(input_buf), date_str=date_str, seed=seed, iters=iters, title=title)
_call
      `);

      const u8 = new Uint8Array(bytes);
      const t0 = performance.now();
      const py_result = callGen(u8, dateStr, seed, iters, title);
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);

      const result = py_result.toJs({ dict_converter: Object.fromEntries });
      py_result.destroy();

      const xlsx = result.xlsx_bytes; // Uint8Array
      postMessage(
        {
          type: "done",
          payload: {
            xlsx,
            review: result.review,
            summary: result.summary,
            elapsed,
          },
        },
        [xlsx.buffer]
      );
    } catch (err) {
      postMessage({ type: "error", msg: String(err) + "\n" + (err.stack || "") });
    }
  }
};
