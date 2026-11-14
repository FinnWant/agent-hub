import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, type Sandbox, type SystemInfo, type Template } from "../lib/api";
import { bytes } from "../lib/format";
import { Dialog, useToast } from "./ui";

const MEMORY_OPTIONS = [
  { value: "512m", bytes: 512 * 2 ** 20 },
  { value: "1g", bytes: 2 ** 30 },
  { value: "2g", bytes: 2 * 2 ** 30 },
  { value: "4g", bytes: 4 * 2 ** 30 },
];
const CPU_OPTIONS = [1, 2, 4];
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$/;
const REPO_RE = /^https:\/\/[A-Za-z0-9.-]+(:\d+)?\/[A-Za-z0-9._~/-]+$/;
const BRANCH_RE = /^[A-Za-z0-9._][A-Za-z0-9._/-]{0,99}$/;

export default function NewSandboxDialog({
  system,
  onClose,
  onCreated,
}: {
  system: SystemInfo | null;
  onClose: () => void;
  onCreated: (sb: Sandbox) => void;
}) {
  const toast = useToast();
  const [templates, setTemplates] = useState<Template[] | null>(null);
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("python");
  const [memory, setMemory] = useState("2g");
  const [cpus, setCpus] = useState(2);
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<{ message: string; reasons: string[] } | null>(null);

  useEffect(() => {
    api.templates().then((t) => {
      setTemplates(t);
      if (!t.find((x) => x.name === "python" && x.built)) {
        const first = t.find((x) => x.built);
        if (first) setTemplate(first.name);
      }
    }).catch(() => setTemplates([]));
  }, []);

  const maxMem = system?.caps.max_sandbox_memory ?? 4 * 2 ** 30;
  const maxCpus = system?.caps.max_sandbox_cpus ?? 4;
  const errors = {
    name: !name ? "Give it a name" : !NAME_RE.test(name) ? "Letters, digits, space, . _ - (max 40)" : null,
    repo: repo && !REPO_RE.test(repo) ? "An https:// URL without credentials" : null,
    branch: branch && !BRANCH_RE.test(branch) ? "Not a valid branch name" : null,
  };
  const invalid = Object.values(errors).some(Boolean);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (invalid) return;
    setBusy(true);
    setFailure(null);
    try {
      const sb = await api.createSandbox({
        name: name.trim(),
        template,
        memory,
        cpus,
        ...(repo ? { repo_url: repo } : {}),
        ...(branch ? { base_branch: branch } : {}),
      });
      if (sb.state === "error") toast("error", `${sb.name} was created but failed to start: ${sb.error}`);
      else toast("success", `${sb.name} is running`);
      onCreated(sb);
    } catch (err) {
      const e2 = err as ApiError;
      setFailure({ message: e2.message, reasons: e2.reasons ?? [] });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      title="New sandbox"
      description="An isolated container with its own /workspace volume. It starts right away."
      onClose={onClose}
      footer={
        <>
          <span className="left">
            {bytes(MEMORY_OPTIONS.find((m) => m.value === memory)!.bytes)} · {cpus} CPU
          </span>
          <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" form="new-sandbox" className="btn btn-primary" disabled={busy || (touched && invalid)}>
            {busy ? "Creating…" : "Create sandbox"}
          </button>
        </>
      }
    >
      <form id="new-sandbox" onSubmit={submit} style={{ display: "contents" }}>
        {!failure && system && system.blockers.length > 0 && (
          <div className="banner banner-warning">
            <div>
              <strong>It may not start right now.</strong>
              <ul>{system.blockers.map((r) => <li key={r}>{r}</li>)}</ul>
            </div>
          </div>
        )}
        {failure && (
          <div className="banner banner-danger" role="alert">
            <div>
              <strong>Couldn't create it.</strong> {failure.reasons.length ? "" : failure.message}
              {failure.reasons.length > 0 && <ul>{failure.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
            </div>
          </div>
        )}
        <div className="field">
          <label htmlFor="sb-name">Name</label>
          <input id="sb-name" className="input" autoFocus placeholder="my-project" value={name} maxLength={40}
            onChange={(e) => setName(e.target.value)} aria-invalid={touched && !!errors.name} />
          {touched && errors.name && <span className="error">{errors.name}</span>}
        </div>

        <div className="field">
          <span className="field-label" id="tpl-label">Template</span>
          <div className="segmented" role="radiogroup" aria-labelledby="tpl-label">
            {(templates ?? [{ name: "python", description: "", built: true }, { name: "node", description: "", built: true }, { name: "static", description: "", built: true }] as Template[]).map((t) => (
              <button key={t.name} type="button" role="radio" aria-checked={template === t.name}
                disabled={!t.built} onClick={() => setTemplate(t.name)}
                title={t.built ? t.description : "Image not built. Run deploy/install.sh"}>
                <span className="mono">{t.name}</span>
                <small>{t.built ? shortDesc(t) : "not built"}</small>
              </button>
            ))}
          </div>
        </div>

        <div className="form-grid">
          <div className="field">
            <span className="field-label" id="mem-label">Memory</span>
            <div className="segmented compact" role="radiogroup" aria-labelledby="mem-label">
              {MEMORY_OPTIONS.filter((m) => m.bytes <= maxMem).map((m) => (
                <button key={m.value} type="button" role="radio" aria-checked={memory === m.value}
                  onClick={() => setMemory(m.value)}>
                  {m.value.toUpperCase()}
                </button>
              ))}
            </div>
          </div>
          <div className="field">
            <span className="field-label" id="cpu-label">CPUs</span>
            <div className="segmented compact" role="radiogroup" aria-labelledby="cpu-label">
              {CPU_OPTIONS.filter((c) => c <= maxCpus).map((c) => (
                <button key={c} type="button" role="radio" aria-checked={cpus === c} onClick={() => setCpus(c)}>
                  {c}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="form-grid">
          <div className="field">
            <label htmlFor="sb-repo">Repository <span className="muted">(optional)</span></label>
            <input id="sb-repo" className="input mono" placeholder="https://github.com/you/repo" value={repo}
              onChange={(e) => setRepo(e.target.value.trim())} aria-invalid={touched && !!errors.repo} />
            {touched && errors.repo && <span className="error">{errors.repo}</span>}
          </div>
          <div className="field">
            <label htmlFor="sb-branch">Base branch</label>
            <input id="sb-branch" className="input mono" placeholder="main" value={branch}
              onChange={(e) => setBranch(e.target.value.trim())} aria-invalid={touched && !!errors.branch} />
            {touched && errors.branch && <span className="error">{errors.branch}</span>}
          </div>
          <span className="hint span muted" style={{ fontSize: 12, marginTop: -8 }}>
            Saved with the sandbox; it's cloned when agent runs arrive.
          </span>
        </div>
      </form>
    </Dialog>
  );
}

function shortDesc(t: Template): string {
  return t.description.split(/,| with /)[0].slice(0, 28);
}
