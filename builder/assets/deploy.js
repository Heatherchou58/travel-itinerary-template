/* ══════════════════════════════════════════════════
   GitHub 部署
   用 Git Data API 把所有檔案包成「一個 commit」，
   而不是用 Contents API 一個檔案一次——那樣會產生 8 個
   commit，而且中途失敗會留下半套檔案。
   ══════════════════════════════════════════════════ */

const API = "https://api.github.com";

/* GitHub 的錯誤訊息是英文的，翻成看得懂的說明 */
function explain(status, body, repo){
  const msg = (body && body.message) || "";
  if (status === 401) return "權杖無效或已過期，請重新產生一把。";
  if (status === 404) return `找不到 ${repo}。請確認名稱是「帳號/repo」的格式，` +
                             "而且權杖的 Repository access 有選到這個 repo。";
  if (status === 403){
    if (/rate limit/i.test(msg)) return "呼叫太頻繁被暫時擋住，等幾分鐘再試。";
    return "權限不足。權杖的 Permissions → Contents 要設成 Read and write。";
  }
  if (status === 409) return "分支名稱不存在，或 repo 還是空的（先在 GitHub 上建立第一個檔案）。";
  if (status === 422) return `GitHub 不接受這次的內容：${msg}`;
  return `GitHub 回傳 ${status}${msg ? "：" + msg : ""}`;
}

async function gh(token, path, options = {}){
  const r = await fetch(API + path, {
    ...options,
    headers: {
      "Authorization": "Bearer " + token,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      ...(options.body ? { "Content-Type": "application/json" } : {})
    }
  });
  let body = null;
  try { body = await r.json(); } catch { /* 204 之類沒有內容 */ }
  if (!r.ok){
    const e = new Error(explain(r.status, body, path.split("/").slice(2, 4).join("/")));
    e.status = r.status;
    throw e;
  }
  return body;
}

/* Blob / ArrayBuffer → base64（大檔案要分段，直接 apply 會爆堆疊）*/
export async function toBase64(data){
  const buf = data instanceof ArrayBuffer ? data : await data.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000)
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(out);
}
/* 文字要先轉成 UTF-8 位元組再 base64，否則中文會壞掉 */
export async function utf8ToBase64(text){
  return toBase64(new TextEncoder().encode(text).buffer);
}

/**
 * files: [{ path:"index.html", content:"...", binary:false }
 *         { path:"icons/icon-192.png", blob:Blob, binary:true }]
 */
export async function deployToGitHub({ token, repo, branch = "main", prefix = "", files, message, onStep }){
  const [owner, name] = repo.split("/").map(s => s.trim());
  if (!owner || !name) throw new Error("儲存庫格式應該是「帳號/repo」，例如 heather/seoul-trip。");
  const base = `/repos/${owner}/${name}`;
  const step = (n, total, t) => onStep && onStep(n, total, t);
  const total = files.length + 5;

  /* 1. 先確認 repo 存不存在。GitHub 對「不存在」和「沒權限」都回 404，
        先分開檢查才能給出有用的錯誤訊息，而不是一律說「找不到分支」。 */
  step(1, total, `確認 ${repo}…`);
  let info;
  try{
    info = await gh(token, base);
  }catch(e){
    if (e.status === 404)
      throw new Error(`存取不到 ${repo}。三個可能：名稱打錯、` +
                      "權杖的 Repository access 沒有選到這個 repo、或這是別人的 repo。");
    throw e;
  }

  /* 2. 目前分支的位置 */
  step(2, total, `讀取 ${branch} 分支…`);
  let ref;
  try{
    ref = await gh(token, `${base}/git/ref/heads/${encodeURIComponent(branch)}`);
  }catch(e){
    if (e.status === 404 || e.status === 409){
      const d = info && info.default_branch;
      throw new Error(`${repo} 裡沒有「${branch}」這個分支。` +
        (d && d !== branch ? `這個 repo 的預設分支是「${d}」，把分支欄改成它試試。`
                           : "如果 repo 還是空的，請先在 GitHub 網頁上建一個 README 再回來。"));
    }
    throw e;
  }
  const parent = ref.object.sha;
  const baseCommit = await gh(token, `${base}/git/commits/${parent}`);

  /* 2. 每個檔案上傳成 blob */
  const tree = [];
  let n = 2;
  for (const f of files){
    step(++n, total, `上傳 ${f.path}…`);
    const content = f.binary ? await toBase64(f.blob) : await utf8ToBase64(f.content);
    const blob = await gh(token, `${base}/git/blobs`, {
      method: "POST",
      body: JSON.stringify({ content, encoding: "base64" })
    });
    const p = (prefix ? prefix.replace(/^\/+|\/+$/g, "") + "/" : "") + f.path;
    tree.push({ path: p, mode: "100644", type: "blob", sha: blob.sha });
  }

  /* 3. 疊在原本的樹上，沒動到的檔案會保留 */
  step(++n, total, "建立檔案樹…");
  const newTree = await gh(token, `${base}/git/trees`, {
    method: "POST",
    body: JSON.stringify({ base_tree: baseCommit.tree.sha, tree })
  });

  /* 4. 一個 commit 包含全部變更 */
  step(++n, total, "建立 commit…");
  const commit = await gh(token, `${base}/git/commits`, {
    method: "POST",
    body: JSON.stringify({ message, tree: newTree.sha, parents: [parent] })
  });

  /* 5. 移動分支 */
  step(++n, total, "更新分支…");
  await gh(token, `${base}/git/refs/heads/${encodeURIComponent(branch)}`, {
    method: "PATCH",
    body: JSON.stringify({ sha: commit.sha, force: false })
  });

  return {
    sha: commit.sha,
    short: commit.sha.slice(0, 7),
    url: `https://github.com/${owner}/${name}/commit/${commit.sha}`,
    files: files.length
  };
}
