import { useCallback, useEffect, useState } from "react";
import { api, type Browse, type FileEntry, type Sandbox } from "../lib/api";
import { bytes } from "../lib/format";
import { Icon } from "./ui";

function join(dir: string, name: string) {
  return dir ? `${dir}/${name}` : name;
}

/** Read-only browser for /workspace: a lazily loaded tree and a viewer. */
export default function Files({ sb }: { sb: Sandbox }) {
  const [dirs, setDirs] = useState<Record<string, Browse>>({});
  const [open, setOpen] = useState<Set<string>>(new Set([""]));
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<Browse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const running = sb.state === "running";

  const loadDir = useCallback(async (path: string) => {
    try {
      const d = await api.files(sb.id, path);
      setDirs((m) => ({ ...m, [path]: d }));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [sb.id]);

  useEffect(() => {
    if (running) loadDir("");
  }, [running, loadDir]);

  const toggle = (path: string) => {
    setOpen((o) => {
      const n = new Set(o);
      if (n.has(path)) n.delete(path);
      else {
        n.add(path);
        if (!dirs[path]) loadDir(path);
      }
      return n;
    });
  };

  const view = async (path: string) => {
    setSelected(path);
    setFile(null);
    try {
      setFile(await api.files(sb.id, path));
    } catch (e) {
      setFile({ path, kind: "other", size: 0, content: undefined, target: (e as Error).message });
    }
  };

  const refresh = () => {
    setDirs({});
    [...open].forEach(loadDir);
    if (selected) view(selected);
  };

  if (!running) {
    return <div className="card empty"><h2>Sandbox is stopped</h2><p>Start it to browse its files.</p></div>;
  }

  const renderDir = (path: string, depth: number): React.ReactNode => {
    const d = dirs[path];
    if (!d) return <div className="tree-row muted" style={{ paddingLeft: 12 + depth * 14 }}>Loading…</div>;
    const entries = (d.entries ?? []).filter((e) => showHidden || !e.name.startsWith("."));
    if (!entries.length) return <div className="tree-row muted" style={{ paddingLeft: 12 + depth * 14 }}>empty</div>;
    return entries.map((e: FileEntry) => {
      const p = join(path, e.name);
      const isDir = e.type === "dir";
      return (
        <div key={p}>
          <button className="tree-row" style={{ paddingLeft: 12 + depth * 14 }} aria-selected={selected === p}
            aria-expanded={isDir ? open.has(p) : undefined}
            onClick={() => (isDir ? toggle(p) : view(p))}>
            <span className={`tree-caret ${isDir ? "" : "hidden"}`}><Icon name="chevron" size={12} /></span>
            <span className={`tree-name ${isDir ? "is-dir" : ""} ${e.type === "link" ? "is-link" : ""}`}>{e.name}</span>
            {!isDir && <span className="tree-size">{e.type === "link" ? "→" : bytes(e.size)}</span>}
          </button>
          {isDir && open.has(p) && renderDir(p, depth + 1)}
        </div>
      );
    });
  };

  return (
    <div className="files">
      <div className="card files-tree">
        <div className="files-bar">
          <span className="mono">/workspace</span>
          <span className="spacer" />
          <label className="muted check">
            <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} /> hidden
          </label>
          <button className="btn btn-ghost btn-sm btn-icon" title="Refresh" aria-label="Refresh" onClick={refresh}>
            <Icon name="refresh" size={14} />
          </button>
        </div>
        {error && <div className="banner banner-danger" style={{ margin: 8 }}><div>{error}</div></div>}
        <div className="tree" role="tree">{renderDir("", 0)}</div>
        {dirs[""]?.truncated && <p className="muted" style={{ padding: "6px 12px" }}>Only the first 2000 entries are shown.</p>}
      </div>
      <div className="card files-view">
        {!selected ? (
          <div className="empty" style={{ height: "100%", justifyContent: "center" }}>
            <p>Pick a file to view it. Files are read-only here.</p>
          </div>
        ) : (
          <>
            <div className="files-bar">
              <span className="mono files-path">{selected}</span>
              <span className="spacer" />
              {file && <span className="muted mono">{bytes(file.size)}</span>}
            </div>
            <FileBody file={file} />
          </>
        )}
      </div>
    </div>
  );
}

function FileBody({ file }: { file: Browse | null }) {
  if (!file) return <div className="file-msg muted">Loading…</div>;
  if (file.kind === "link") return <div className="file-msg">Symbolic link to <span className="mono">{file.target}</span></div>;
  if (file.binary) return <div className="file-msg muted">Binary file, not shown.</div>;
  if (file.too_large) return <div className="file-msg muted">Larger than {bytes(file.limit ?? 0)}; open it in the terminal instead.</div>;
  if (file.content === undefined) return <div className="file-msg" style={{ color: "var(--status-danger)" }}>{file.target ?? "Can't show this file."}</div>;
  const lines = file.content.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  return (
    <div className="code" role="region" aria-label="File contents">
      <pre className="code-gutter" aria-hidden>{lines.map((_, i) => i + 1).join("\n")}</pre>
      <pre className="code-text">{lines.join("\n")}</pre>
    </div>
  );
}
