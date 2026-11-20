r"""codex-worker：把成块任务派给本机 Codex CLI（codex exec），在指定 git 工作树里执行。

用法：
  codex-worker <工作树> "任务"                     新会话
  codex-worker --resume <会话ID> <工作树> "追加"    续跑已有会话
  codex-worker --review <工作树> <基准> [审查角度]  审查工作树当前分支相对 <基准>（分支或提交）的改动；
                                                   底层是 codex exec review --base，只读沙箱。
                                                   审查角度可选，写法同任务（文本 / - / @文件），用于第二轮按改动特点
                                                   指定角度（见 SKILL.md 的审查规则）。codex exec review 的 --base 不能和
                                                   自定义指令同用（实测报错），所以带角度时改走普通 codex exec + 只读沙箱
                                                   + 审查前言（范围 git diff <基准>...HEAD）
  任务/追加也可以写成 -（从 stdin 读）或 @文件路径（UTF-8 文件）。
  长任务、含引号或以 / 开头的文本，用这两种形式最稳。

行为：
  - 沙箱固定 workspace-write：只能写工作树（和临时目录），执行的命令没有网络；
    审批策略 never，不会卡在等待确认上。
  - 工作树必须是 `git worktree add` 建出的链接工作树：它的 git 数据在主仓库 .git 下，
    不在可写范围内，Codex 因此提交不了。主检出（.git 是目录）默认拒绝运行，
    确需运行时设 CODEX_WORKER_ALLOW_MAIN=1。
  - 启动前清掉 http(s)_proxy / all_proxy。shell 里带的本地代理端口会让 Codex 判定
    离线沙箱的防火墙规则要变更，于是拉起提权程序弹 UAC（无人点击即报 1223，之后 shell
    命令全部失败）；同一个代理还会让 Responses WebSocket 握手失败、长时间重连。
    沙箱日志里的特征是 "offline firewall settings changed (stored_ports=..., desired_ports=...)"。
  - 项目说明上限调到 128KB（project_doc_max_bytes）：Codex 默认只加载 AGENTS.md 前 32KB，
    大型项目的 AGENTS.md 常超过这个长度（实测有 70KB 以上的），会被截掉一半。
  - Maven 项目（工作树根目录有 pom.xml）自动配好离线测试环境，让 Codex 能自己跑 `mvn -o`：
    · MAVEN_OPTS 加 -Dmaven.repo.local=<D 盘的 Maven 仓库副本>，派活前用 robocopy 从本账户
      ~/.m2/repository 增量同步（只复制新增或更新的文件，通常 1 秒内）。两个原因：
      沙箱用独立的 Windows 账户执行命令，它的 ~/.m2 是空的，离线时连父 POM 都找不到；
      而直接指向本账户 ~/.m2 时，沙箱对用户主目录下的路径调 toRealPath 会被拒绝，javac 关闭
      多版本 jar（如 okio-jvm）时正要调它，编译报 AccessDeniedException。实测：同一个全新
      工作树，指向 ~/.m2 连续 3 次失败，指向主目录之外的副本两次干净编译都通过。
    · JAVA_TOOL_OPTIONS 加 -Djava.io.tmpdir=<工作树>/target/codex-tmp（仅当 target/ 已被 git 忽略）。
      JUnit @TempDir 默认建在本账户 %TEMP% 下，沙箱账户删不掉这层目录，测试收尾报
      AccessDeniedException。JAVA_TOOL_OPTIONS 同时作用于 Maven 和 surefire 分出的测试 JVM。
    实测：配好后沙箱内 mvn -o 跑单测全绿、临时目录无残留。
  - 完整事件流写到 ~/.codex-worker/logs/；标准输出是精简进度和结束摘要，
    最后一行固定是「会话ID: <id>」。

环境变量：
  CODEX_EXE              codex.exe 路径；默认按 Appx 包 OpenAI.Codex 解析
  CODEX_WORKER_MODEL     派活 / 续跑的模型，默认 gpt-6.2-sol
  CODEX_WORKER_EFFORT    派活 / 续跑的思考强度，默认 max
  CODEX_WORKER_REVIEW_MODEL   --review 的模型，默认 gpt-6.2-astra（与实现用不同模型，交叉检查）
  CODEX_WORKER_REVIEW_EFFORT  --review 的思考强度，默认 xhigh
  CODEX_WORKER_M2_REPO   直接指定给 Codex 用的 Maven 仓库路径（设了就不同步副本）
  CODEX_WORKER_M2_MIRROR Maven 仓库副本位置；默认 D:\codex-m2\repository（必须在用户主目录之外）
  CODEX_WORKER_ALLOW_MAIN=1  允许在主检出运行（不推荐）
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

USAGE = (
    "用法：codex-worker <工作树> \"任务\"\n"
    "      codex-worker --resume <会话ID> <工作树> \"追加\"\n"
    "      codex-worker --review <工作树> <基准分支或提交> [审查角度]\n"
    "      任务/追加可写成 -（读 stdin）或 @文件路径"
)

PROXY_VARS = {"http_proxy", "https_proxy", "all_proxy"}

PREAMBLE = """[codex-worker 约束]
- 这是非交互运行，没有人会回答你的提问。不要停下来提问或等待确认；需要派活方确认或补充的事项（缺失的日志、有歧义的需求等）写进交付说明，其余工作按最合理的判断继续完成。
- 你在 git 工作树 {wt} 中工作，这是唯一可写目录，不要修改它以外的文件。
- 沙箱禁止联网和提交：不要尝试 git add/commit/push、下载依赖或访问网络。需要联网才能完成的验证（例如 Maven 首次拉依赖）写进交付说明，由派活方执行。
{maven}- 完成后用简体中文给出交付说明：改动文件清单、关键设计取舍、已运行的验证及结果、未完成项与风险。

[任务]
"""

REVIEW_PREAMBLE = """[codex-worker 审查]
- 这是非交互的只读代码审查，没有人会回答你的提问。沙箱只读：不要修改文件，也不要编译或跑测试。
- 审查范围：当前工作树 HEAD 相对 {base} 的改动。先看 `git log {base}..HEAD` 和 `git diff {base}...HEAD`，再按需阅读相关代码与调用方。
- 只报告本次改动引入或暴露的、能说清触发条件的问题。每条写明：代码位置（文件:行）、触发条件、影响、严重程度（高/中/低）、修复建议。推测性的担忧单独列为「待确认」，并说明需要什么证据才能确认。没有发现问题就明确说没有。
- 用简体中文输出。

[审查角度]
"""

MAVEN_HINT = (
    "- 这是 Maven 项目：本地仓库已通过 MAVEN_OPTS 指向派活方同步好的仓库副本，Java 临时目录指向 target/codex-tmp。"
    "用 `mvn -o` 离线只跑与改动直接相关的测试；缺依赖导致跑不起来时写进交付说明。"
    "尽量不用 mvn clean（会删掉 target/codex-tmp），确需 clean 时先重建该目录再跑测试。"
    "编译若报某个依赖 JAR 的 AccessDeniedException，不要排查沙箱权限，写进交付说明即可。\n"
)


def out(line=""):
    print(line, flush=True)


def die(msg, code=2):
    print(f"[codex-worker] 错误：{msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def parse_args(argv):
    """返回 (模式, 会话ID, 工作树, 任务或基准, 审查角度)；模式为 new / resume / review。"""
    if argv and argv[0] in ("-h", "--help"):
        out(USAGE)
        sys.exit(0)
    if argv and argv[0] == "--review":
        if len(argv) not in (3, 4):
            die(USAGE)
        return "review", None, argv[1], argv[2], (argv[3] if len(argv) == 4 else None)
    if argv and argv[0] == "--resume":
        if len(argv) != 4:
            die(USAGE)
        return "resume", argv[1], argv[2], argv[3], None
    if len(argv) != 2:
        die(USAGE)
    return "new", None, argv[0], argv[1], None


def check_base(wt, base):
    r = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        die(f"基准不存在：{base}（先 git fetch，或传分支名/提交号）")
    return base


def read_task(spec):
    try:
        if spec == "-":
            text = sys.stdin.buffer.read().decode("utf-8")
        elif spec.startswith("@"):
            text = Path(spec[1:]).read_text(encoding="utf-8")
        else:
            text = spec
    except (OSError, UnicodeDecodeError) as e:
        die(f"读取任务失败：{e}")
    text = text.strip()
    if not text:
        die("任务内容为空")
    return text


def check_worktree(path):
    wt = Path(path).resolve()
    if not wt.is_dir():
        die(f"工作树不存在：{wt}")
    r = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        die(f"不是 git 工作树：{wt}")
    top = Path(r.stdout.strip()).resolve()
    if os.path.normcase(str(top)) != os.path.normcase(str(wt)):
        die(f"请传工作树根目录 {top}（收到的是 {wt}）")
    if (wt / ".git").is_dir() and os.environ.get("CODEX_WORKER_ALLOW_MAIN") != "1":
        die(
            "这是主检出（.git 是目录），在这里无法保证拦住 Codex 提交。"
            "请先 `git worktree add <路径> -b <分支>`，或设 CODEX_WORKER_ALLOW_MAIN=1 强制运行"
        )
    return wt


def find_codex():
    # 不用 ~/.codex/.sandbox-bin/codex.exe：那是沙箱辅助程序的落地副本，版本会落后于应用本体。
    p = os.environ.get("CODEX_EXE")
    if p:
        if Path(p).is_file():
            return p
        die(f"CODEX_EXE 指向的文件不存在：{p}")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-AppxPackage OpenAI.Codex | Select-Object -First 1).InstallLocation"],
        capture_output=True, text=True,
    )
    loc = r.stdout.strip()
    exe = Path(loc) / "app" / "resources" / "codex.exe" if loc else None
    if not exe or not exe.is_file():
        die("找不到 Codex CLI（未安装 Codex 桌面应用？）。可设 CODEX_EXE 指定 codex.exe")
    return str(exe)


def current_account():
    """当前登录的 Codex 账号：邮箱打码 + 套餐，例如 ab***@gmail.com（plus）。

    auth.json 只存一个账号，在桌面应用里切号会直接覆盖它；每次派活都新起 codex 进程，
    启动时读到的就是当时登录的账号。这里只在本地解 id_token 的 payload，不发请求、不输出 token。
    """
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        tokens = auth.get("tokens") or {}
        if not tokens.get("id_token"):
            return "API Key" if auth.get("OPENAI_API_KEY") else "未登录"
        payload = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        local, _, domain = (claims.get("email") or "").partition("@")
        masked = f"{local[:2]}***@{domain}" if domain else "未知邮箱"
        plan = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
        return f"{masked}（{plan}）" if plan else masked
    except Exception:
        return "未知（auth.json 读取或解析失败）"


def warn(msg):
    print(f"[codex-worker] 提示：{msg}", file=sys.stderr, flush=True)


def sync_m2_mirror():
    """把本账户 ~/.m2/repository 增量同步到用户主目录之外的副本，返回给 Codex 用的仓库路径。

    沙箱对用户主目录下的路径调 toRealPath 会被拒绝（javac 关闭多版本 jar 时调用），
    副本放 D 盘即可避开。同步失败时退回本账户 ~/.m2，并提示编译可能报 AccessDeniedException。
    """
    src = Path.home() / ".m2" / "repository"
    dst = Path(os.environ.get("CODEX_WORKER_M2_MIRROR") or r"D:\codex-m2\repository")
    if not src.is_dir():
        return str(src)
    # /E 含子目录，/XO 不覆盖副本里更新的文件；不加 /PURGE，副本只增不删。
    r = subprocess.run(
        ["robocopy", str(src), str(dst), "/E", "/XO", "/R:1", "/W:1", "/MT:8",
         "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
        capture_output=True,
    )
    if r.returncode >= 8:   # robocopy 退出码 0~7 都算成功（位标志），8 及以上是失败
        warn(f"Maven 仓库副本同步失败（robocopy 退出码 {r.returncode}），改用 {src}；"
             "沙箱里编译可能报依赖 JAR 的 AccessDeniedException")
        return str(src)
    return str(dst)


def setup_maven(wt, env):
    """Maven 项目才生效：改 env，返回要加进约束前言的说明；非 Maven 项目返回空串。

    两个环境变量都按空格切分参数，路径带空格时跳过，免得把 JVM 参数切坏。
    """
    if not (wt / "pom.xml").is_file():
        return ""
    repo = os.environ.get("CODEX_WORKER_M2_REPO") or sync_m2_mirror()
    opts = env.get("MAVEN_OPTS", "")
    if "maven.repo.local" in opts:
        pass
    elif not Path(repo).is_dir():
        warn(f"Maven 本地仓库不存在，沙箱里将无法离线构建：{repo}")
    elif " " in repo:
        warn(f"Maven 本地仓库路径含空格，未设置 MAVEN_OPTS：{repo}")
    else:
        env["MAVEN_OPTS"] = f"{opts} -Dmaven.repo.local={repo}".strip()

    tmp = wt / "target" / "codex-tmp"
    ignored = subprocess.run(
        ["git", "-C", str(wt), "check-ignore", "-q", "target/codex-tmp"],
        capture_output=True,
    ).returncode == 0
    jto = env.get("JAVA_TOOL_OPTIONS", "")
    if "java.io.tmpdir" in jto:
        pass
    elif not ignored:
        warn("target/ 未被 git 忽略，未改 Java 临时目录；JUnit @TempDir 收尾可能报 AccessDeniedException")
    elif " " in str(tmp):
        warn(f"工作树路径含空格，未改 Java 临时目录：{tmp}")
    else:
        tmp.mkdir(parents=True, exist_ok=True)
        env["JAVA_TOOL_OPTIONS"] = f"{jto} -Djava.io.tmpdir={tmp}".strip()
    return MAVEN_HINT


CMD_RE = re.compile(r"-Command\s+'(.*)'\s*$", re.S)

# 额度用尽（实测原文）：error 与 turn.failed 各来一条，
# "You’ve hit your usage limit. Upgrade to Pro (...) ... or try again at 6:57 PM."
USAGE_LIMIT_RE = re.compile(r"usage limit", re.I)
RETRY_AT_RE = re.compile(r"try again at\s+(.+?)\s*\.?\s*$", re.I)
# 额度用尽专用退出码（EX_TEMPFAIL），与普通失败区分，派活方据此切换账号或降级给 subagent。
EXIT_USAGE_LIMIT = 75


def short(s, n=200):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


class Run:
    def __init__(self):
        self.sid = None
        self.last_message = ""
        self.usage = None
        self.errors = []
        self.usage_limit = None   # 额度用尽时为恢复时间文本（报错没给时间则为空串）

    def handle(self, raw):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            out(line)
            return
        t = ev.get("type")
        if t == "thread.started":
            self.sid = ev.get("thread_id")
            out(f"[codex-worker] 会话ID: {self.sid}")
        elif t == "item.completed":
            self.on_item(ev.get("item") or {})
        elif t == "turn.completed":
            self.usage = ev.get("usage")
        elif t in ("turn.failed", "error"):
            err = ev.get("error")
            msg = err.get("message") if isinstance(err, dict) else ev.get("message", line)
            self.errors.append(msg)
            out(f"[error] {short(msg, 500)}")
            if msg and USAGE_LIMIT_RE.search(msg):
                m = RETRY_AT_RE.search(msg)
                self.usage_limit = m.group(1) if m else ""

    def on_item(self, it):
        kind = it.get("type")
        if kind == "agent_message":
            self.last_message = it.get("text", "")
            out("[codex] " + self.last_message.rstrip())
        elif kind == "command_execution":
            cmd = it.get("command", "")
            m = CMD_RE.search(cmd)
            out(f"[cmd exit={it.get('exit_code')}] {short(m.group(1) if m else cmd)}")
        elif kind == "file_change":
            for ch in it.get("changes") or []:
                out(f"[file {ch.get('kind')}] {ch.get('path')}")
        elif kind == "error":
            self.errors.append(it.get("message", ""))
            out(f"[error] {short(it.get('message', ''), 500)}")
        elif kind == "reasoning":
            pass
        else:
            name = it.get("tool") or it.get("name") or ""
            out(f"[{kind}] {name}".rstrip())


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    mode, sid, wt_arg, spec, angle_spec = parse_args(sys.argv[1:])
    wt = check_worktree(wt_arg)
    if mode == "review":
        base = check_base(wt, spec)
        angle = read_task(angle_spec) if angle_spec else ""
    else:
        task = read_task(spec)
    codex = find_codex()

    # 实现用 Sol / max，审查用 Astra / xhigh，让审查天然跨模型。
    # 两组环境变量各管各的，审查不读 CODEX_WORKER_MODEL，免得为派活临时改模型时把审查也带偏。
    if mode == "review":
        model = os.environ.get("CODEX_WORKER_REVIEW_MODEL", "gpt-6.2-astra")
        effort = os.environ.get("CODEX_WORKER_REVIEW_EFFORT", "xhigh")
    else:
        model = os.environ.get("CODEX_WORKER_MODEL", "gpt-6.2-sol")
        effort = os.environ.get("CODEX_WORKER_EFFORT", "max")
    # exec resume / review 不接受 -s/-C，统一用 -c 覆盖配置 + 进程 cwd 指定工作目录。
    # 审查只读代码，用只读沙箱，也不需要 Maven 环境。
    sandbox = "read-only" if mode == "review" else "workspace-write"
    common = [
        "--json", "-m", model,
        "-c", f'model_reasoning_effort="{effort}"',
        "-c", f'sandbox_mode="{sandbox}"',
        "-c", 'approval_policy="never"',
        "-c", "project_doc_max_bytes=131072",
    ]
    env = {k: v for k, v in os.environ.items() if k.lower() not in PROXY_VARS}
    maven_hint = "" if mode == "review" else setup_maven(wt, env)

    if mode == "review" and angle:
        # exec review 的 --base 与自定义指令互斥，带角度时用普通 exec（沙箱已是只读）。
        cmd = [codex, "exec", *common, "-C", str(wt), "-"]
        prompt = REVIEW_PREAMBLE.format(base=base) + angle
    elif mode == "review":
        cmd = [codex, "exec", "review", *common, "--base", base]
        prompt = ""
    elif mode == "resume":
        cmd = [codex, "exec", "resume", *common, sid, "-"]
        prompt = task
    else:
        cmd = [codex, "exec", *common, "-C", str(wt), "-"]
        prompt = PREAMBLE.format(wt=wt, maven=maven_hint) + task

    log_dir = Path.home() / ".codex-worker" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"{stamp}.jsonl"
    err_path = log_dir / f"{stamp}.stderr.txt"

    account = current_account()
    label = {"new": "新会话", "resume": f"续跑 {sid}",
             "review": f"审查（基准 {spec}{'，指定角度' if mode == 'review' and angle else ''}）"}[mode]
    out(f"[codex-worker] {label}  工作树: {wt}  模型: {model}/{effort}"
        f"{'  Maven 离线环境已配置' if maven_hint else ''}")
    out(f"[codex-worker] 账号: {account}")
    run = Run()
    start = time.time()
    interrupted = False
    with open(log_path, "wb") as logf, open(err_path, "wb") as errf:
        proc = subprocess.Popen(
            cmd, cwd=str(wt), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf,
        )
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
            for raw in proc.stdout:
                logf.write(raw)
                logf.flush()
                run.handle(raw)
            rc = proc.wait()
        except KeyboardInterrupt:
            interrupted = True
            proc.kill()
            rc = proc.wait()
    elapsed = int(time.time() - start)

    status = subprocess.run(
        ["git", "-C", str(wt), "status", "--short", "--untracked-files=all"],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.rstrip().splitlines()

    out()
    out("==== codex-worker 结束 ====")
    tokens = ""
    if run.usage:
        u = run.usage
        if not any(u.get(k) for k in ("input_tokens", "output_tokens")):
            # codex exec review 的 turn.completed 上报全 0（实测），并非没消耗额度。
            tokens = "  tokens: 未上报"
        else:
            tokens = (f"  tokens: 输入 {u.get('input_tokens')}（缓存 {u.get('cached_input_tokens')}）"
                      f" / 输出 {u.get('output_tokens')}")
    out(f"退出码: {rc}{'（已中断）' if interrupted else ''}  用时: {elapsed}s{tokens}")
    out(f"账号: {account}")
    if run.errors:
        out("错误:")
        for e in dict.fromkeys(run.errors):   # 去重：额度用尽等报错会以 error + turn.failed 各来一条
            out(f"  - {short(e, 500)}")
    if run.usage_limit is not None:
        out(f"Codex 额度用尽：恢复时间 {run.usage_limit or '（报错未给出）'}。"
            f"按 SKILL.md：切换账号后重派，或改交子代理；审查则如实告知用户。")
    if rc != 0:
        tail = err_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
        if tail:
            out("stderr 末尾:")
            for t in tail:
                out(f"  {t}")
    out("最终答复:")
    out(run.last_message.rstrip() or "（无）")
    out("工作树改动（git status --short）:")
    if status:
        for s in status[:100]:
            out(f"  {s}")
        if len(status) > 100:
            out(f"  …共 {len(status)} 项")
    else:
        out("  （无改动）")
    out(f"日志: {log_path}")
    out(f"会话ID: {run.sid or '（未获取到）'}")
    if run.usage_limit is not None:
        sys.exit(EXIT_USAGE_LIMIT)
    sys.exit(rc if rc else (0 if run.sid else 1))


if __name__ == "__main__":
    main()
