// Minimal GitHub REST client for the Configure page. All calls run in the
// browser with a user-supplied fine-grained token (stored only in localStorage).
// The repo is public, so reads work even without a token; writes need one with
// Contents: Read & Write (and Actions: R&W for "Run now").

export const REPO = {
  owner: "benschwartz42",
  name: "policy-signal",
  branch: "main",
  configPath: "news-digest/config.yaml",
  seenPath: "news-digest/state/seen.json",
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
    if (res.status === 403 && opts.method && opts.method !== "GET") {
      throw new Error(
        "GitHub 403 — your token can't write to this repo. Create a fine-grained " +
        "token with “Only select repositories → policy-signal” (NOT “Public " +
        "repositories”), and Repository permissions: Contents = Read and write, " +
        "Actions = Read and write. Then paste it again."
      );
    }
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

// How many dedup entries are currently remembered (0 if the store is absent).
// The store keys both the URL and the story of each delivered item, so the
// entry count is roughly twice the number of distinct items sent.
export async function getSeenCount(token) {
  try {
    const data = await gh(
      `/repos/${REPO.owner}/${REPO.name}/contents/${REPO.seenPath}?ref=${REPO.branch}`,
      token
    );
    const obj = JSON.parse(b64decode(data.content) || "{}");
    return Object.keys(obj).length;
  } catch (e) {
    if (String(e.message).includes("404")) return 0;
    throw e;
  }
}

// Clear the seen-store so the next run re-surfaces the recent backlog.
export async function resetSeenStore(token) {
  let sha = null;
  try {
    const data = await gh(
      `/repos/${REPO.owner}/${REPO.name}/contents/${REPO.seenPath}?ref=${REPO.branch}`,
      token
    );
    sha = data.sha;
    if (b64decode(data.content).trim() === "{}") return { alreadyEmpty: true };
  } catch (e) {
    if (String(e.message).includes("404")) return { alreadyEmpty: true };
    throw e;
  }
  await gh(`/repos/${REPO.owner}/${REPO.name}/contents/${REPO.seenPath}`, token, {
    method: "PUT",
    body: JSON.stringify({
      message: "chore(state): reset sent history via Configure page",
      content: b64encode("{}\n"),
      sha,
      branch: REPO.branch,
    }),
  });
  return { reset: true };
}

// Trigger the daily digest workflow. Pass inputs (e.g. {test_recipients}) for
// the test-send / preview modes.
export async function dispatchWorkflow(token, inputs = {}) {
  return gh(
    `/repos/${REPO.owner}/${REPO.name}/actions/workflows/${REPO.workflow}/dispatches`,
    token,
    { method: "POST", body: JSON.stringify({ ref: REPO.branch, inputs }) }
  );
}

// Sanity-check the token + that it can see the repo.
export async function whoami(token) {
  return gh(`/user`, token);
}
