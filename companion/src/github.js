// Minimal GitHub REST client for the Configure page. All calls run in the
// browser with a user-supplied fine-grained token (stored only in localStorage).
// The repo is public, so reads work even without a token; writes need one with
// Contents: Read & Write (and Actions: R&W for "Run now").

export const REPO = {
  owner: "benschwartz42",
  name: "policy-signal",
  branch: "main",
  configPath: "news-digest/config.yaml",
  workflow: "daily-digest.yml",
};

const API = "https://api.github.com";

// UTF-8 safe base64 <-> string
function b64encode(str) {
  return btoa(unescape(encodeURIComponent(str)));
}
function b64decode(b64) {
  return decodeURIComponent(escape(atob(b64.replace(/\n/g, ""))));
}

function headers(token) {
  const h = { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" };
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

async function gh(path, token, opts = {}) {
  const res = await fetch(`${API}${path}`, { ...opts, headers: { ...headers(token), ...(opts.headers || {}) } });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).message; } catch { /* ignore */ }
    throw new Error(`GitHub ${res.status}: ${detail || res.statusText}`);
  }
  return res.status === 204 ? null : res.json();
}

// Read the raw config file + its blob sha (sha is required to update it).
export async function getConfigFile(token) {
  const data = await gh(
    `/repos/${REPO.owner}/${REPO.name}/contents/${REPO.configPath}?ref=${REPO.branch}`,
    token
  );
  return { text: b64decode(data.content), sha: data.sha };
}

// Commit a new config file body.
export async function putConfigFile(token, text, sha, message) {
  return gh(`/repos/${REPO.owner}/${REPO.name}/contents/${REPO.configPath}`, token, {
    method: "PUT",
    body: JSON.stringify({
      message: message || "chore(config): update via Configure page",
      content: b64encode(text),
      sha,
      branch: REPO.branch,
    }),
  });
}

// Trigger the daily digest workflow now.
export async function dispatchWorkflow(token) {
  return gh(
    `/repos/${REPO.owner}/${REPO.name}/actions/workflows/${REPO.workflow}/dispatches`,
    token,
    { method: "POST", body: JSON.stringify({ ref: REPO.branch }) }
  );
}

// Sanity-check the token + that it can see the repo.
export async function whoami(token) {
  return gh(`/user`, token);
}
