const GiB = 2 ** 30;
const MiB = 2 ** 20;

export function bytes(n: number, digits = 1): string {
  if (n >= GiB) return `${(n / GiB).toFixed(n % GiB === 0 ? 0 : digits)} GB`;
  if (n >= MiB) return `${Math.round(n / MiB)} MB`;
  return `${Math.round(n / 1024)} KB`;
}

export function gib(n: number, digits = 1): string {
  return (n / GiB).toFixed(digits);
}

export function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)}d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

export function clock(ts: number): string {
  const d = new Date(ts * 1000);
  const today = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  if (d.toDateString() === today.toDateString()) return time;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

export function repoLabel(url: string | null, branch: string | null): string | null {
  if (!url) return null;
  const short = url.replace(/^https:\/\/(www\.)?github\.com\//, "").replace(/\.git$/, "");
  return branch ? `${short} @ ${branch}` : short;
}
