"""Label mutation patches by running the full suite under pytest.
usage: label.py PRISTINE_CHECKOUT MUT_DIR TESTS_JSON"""
import json, os, shutil, subprocess, sys, time
src, mdir, tj = sys.argv[1:4]
tests = json.load(open(tj))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests: by_file.setdefault(t["file"], []).append(t["number"])
ans_path = os.path.join(mdir, "answers.json")
answers = json.load(open(ans_path))["commits"] if os.path.exists(ans_path) else {}
kinds = json.load(open(os.path.join(mdir, "kinds.json")))
for f in sorted(os.listdir(os.path.join(mdir, "commits"))):
    cid = f[:-6]
    if cid in answers: continue
    work = "/tmp/lab_" + cid
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src, work, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    r = subprocess.run(["git", "apply", os.path.join(mdir, "commits", f)], cwd=work, capture_output=True, text=True)
    if r.returncode:
        print(cid, "apply failed", r.stderr, flush=True); continue
    out = f"/tmp/label_{cid}.json"
    t0 = time.time()
    env = {**os.environ, "PYTHONPATH": work + ":/home/claude/br/build", "LABEL_OUT": out}
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "labelplug", "-p", "no:cacheprovider", "-n", "2",
                    "--timeout", "300", "tests"], cwd=work, env=env, capture_output=True, text=True)
    lab = json.load(open(out))
    failing = set()
    unknown = []
    for nid in lab["failed"]:
        if nid in num: failing.add(num[nid])
        else: unknown.append(nid)
    for bf in lab["bad_files"]:
        for n in by_file.get(bf, []): failing.add(n)
    answers[cid] = {"failing": sorted(failing), "kind": " | ".join(kinds.get(cid, []))}
    json.dump({"commits": answers}, open(ans_path, "w"), indent=0)
    print(cid, len(failing), f"{time.time()-t0:.0f}s", kinds.get(cid), unknown[:3], flush=True)
    shutil.rmtree(work, ignore_errors=True)
