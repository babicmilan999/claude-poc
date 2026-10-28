#!/usr/bin/env python3
"""Validates infra/ and knowledge-base/ without starting anything.

    python3 infra/scripts/validate.py          (needs PyYAML: pip install pyyaml,
    uv run --with pyyaml infra/scripts/validate.py)

Checks:
  1. Compose: for every LLM profile x Compose profile combination, `docker compose
     config` succeeds (skipped with a notice if the Docker CLI is missing; no daemon
     is needed), and a static check of the file with PyYAML: required services, the
     `ollama` service only behind its profile, pinned images, Makefile INFRA_SERVICES
     exist, LiteLLM gets no Langfuse settings.
  2. LiteLLM profiles: each defines exactly the five aliases, with pinned models,
     keys from the environment and no Langfuse (or other) logging callbacks.
  3. Knowledge base: 10-15 articles, one H1 each, every topic of architecture
     section 4 covered, every customer-visible reason code in the banking contract
     explained in an article, no internal codes, no broken links between articles.

Exits 1 on the first failing section's errors (all errors are printed).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("validate.py needs PyYAML: pip install pyyaml (or: uv run --with pyyaml infra/scripts/validate.py)")

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "infra" / "docker-compose.yml"
LITELLM_DIR = ROOT / "infra" / "litellm"
KB_DIR = ROOT / "knowledge-base"
CONTRACT = ROOT / "contracts" / "banking-api.yaml"

ALIASES = {"triage", "assistant", "reasoning", "simulator", "judge"}
LLM_PROFILES = ["ollama", "anthropic", "mixed"]
COMPOSE_PROFILES: list[list[str]] = [[], ["ollama"]]

REQUIRED_SERVICES = {
    "postgres", "banking-service", "agent-service", "ops-console", "customer-app",
    "litellm", "ollama",
    "langfuse-web", "langfuse-worker", "langfuse-postgres", "clickhouse", "redis", "minio",
}
# Images that cannot be pinned by tag (documented in docker-compose.yml).
UNPINNABLE_IMAGES = {"cgr.dev/chainguard/minio:latest"}

# Architecture section 4, "Knowledge base": topic -> article slug(s).
KB_TOPICS = {
    "why cards get declined": ["why-cards-get-declined"],
    "card controls and limits": ["card-controls-and-limits", "daily-payment-limit"],
    "how and when scheduled payments run": ["how-scheduled-payments-work", "why-a-scheduled-payment-failed"],
    "how direct debits and mandates work": ["how-direct-debits-work", "why-a-direct-debit-failed"],
    "fees": ["fees-and-charges"],
    "how to contact support": ["contact-support"],
}

# Every reason code in the banking contract -> the article that explains it to a
# customer. A new enum value in the contract fails this check until it is mapped.
REASON_ARTICLES = {
    "DeclineReason": {
        "CARD_FROZEN": "freeze-and-unfreeze-your-card",
        "CARD_BLOCKED": "lost-stolen-or-blocked-cards",
        "CARD_EXPIRED": "expired-cards-and-replacements",
        "INSUFFICIENT_FUNDS": "balance-and-available-funds",
        "CONTACTLESS_LIMIT_EXCEEDED": "card-controls-and-limits",
        "MERCHANT_CATEGORY_BLOCKED": "card-controls-and-limits",
        "ONLINE_PAYMENTS_DISABLED": "card-controls-and-limits",
        "FRAUD_RULE_TRIGGERED": "security-checks-on-card-payments",
        "ACCOUNT_NOT_ACTIVE": "account-frozen-or-closed",
    },
    "PaymentFailureReason": {
        "INSUFFICIENT_FUNDS": "why-a-scheduled-payment-failed",
        "DAILY_PAYMENT_LIMIT_EXCEEDED": "daily-payment-limit",
        "PAYEE_ACCOUNT_CLOSED": "why-a-scheduled-payment-failed",
        "INVALID_PAYEE_DETAILS": "why-a-scheduled-payment-failed",
        "ACCOUNT_NOT_ACTIVE": "account-frozen-or-closed",
    },
    "CollectionFailureCode": {
        "MANDATE_CANCELLED": "why-a-direct-debit-failed",
        "NO_MANDATE": "why-a-direct-debit-failed",
        "INSUFFICIENT_FUNDS": "why-a-direct-debit-failed",
        "ACCOUNT_CLOSED_BY_ORIGINATOR": "why-a-direct-debit-failed",
        "PAYER_ACCOUNT_CLOSED": "account-frozen-or-closed",
    },
    "CardStatus": {
        "active": "freeze-and-unfreeze-your-card",
        "frozen": "freeze-and-unfreeze-your-card",
        "blocked": "lost-stolen-or-blocked-cards",
        "expired": "expired-cards-and-replacements",
    },
    "MandateStatus": {
        "active": "how-direct-debits-work",
        "cancelled": "how-direct-debits-work",
        "dormant": "how-direct-debits-work",
    },
}

# Internal details that must never appear in customer-facing text.
INTERNAL_PATTERNS = [
    (re.compile(r"\b[A-Z]+(?:_[A-Z]+)+\b"), "an internal reason code"),
    (re.compile(r"\bFR-\d+\b"), "a fraud rule ID"),
    (re.compile(r"risk score", re.I), "risk scores"),
    (re.compile(r"fraud rule", re.I), "fraud rules"),
    (re.compile(r"\b(?:llm|litellm|langfuse|ollama|claude|anthropic)\b", re.I), "internal system names"),
]


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, message: str) -> None:
        if not ok:
            self.errors.append(message)


def load_yaml(path: Path):
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- 1. Compose ----------------------------------------------------------------

def check_compose(r: Report) -> None:
    compose = load_yaml(COMPOSE_FILE)
    services = compose.get("services", {})
    r.check(REQUIRED_SERVICES <= services.keys(),
            f"compose: missing services {sorted(REQUIRED_SERVICES - services.keys())}")

    for name, svc in services.items():
        profiles = svc.get("profiles", [])
        if name == "ollama":
            r.check(profiles == ["ollama"], "compose: `ollama` must only run with the `ollama` Compose profile")
        else:
            r.check(not profiles, f"compose: {name} must not be behind a Compose profile")
        image = svc.get("image")
        if image:
            tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
            pinned = bool(re.search(r"\d", tag)) and tag != "latest"
            r.check(pinned or image in UNPINNABLE_IMAGES, f"compose: {name} image {image} is not pinned")
        else:
            r.check("build" in svc, f"compose: {name} has neither image nor build")

    litellm = services.get("litellm", {})
    command = " ".join(litellm.get("command", []))
    r.check("/app/profiles/${LLM_PROFILE:-ollama}.yaml" in command,
            "compose: litellm must load infra/litellm/${LLM_PROFILE}.yaml")
    env = litellm.get("environment", {})
    r.check(not any(k.startswith("LANGFUSE") for k in env),
            "compose: litellm must not get LANGFUSE_* settings (its own Langfuse logging stays off)")

    # Services `make infra` starts must exist.
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(r"^INFRA_SERVICES \?= (.+)$", makefile, re.M)
    if m:
        missing = [s for s in m.group(1).split() if s not in services]
        r.check(not missing, f"Makefile: INFRA_SERVICES not in compose: {missing}")

    for profile in LLM_PROFILES:
        r.check((LITELLM_DIR / f"{profile}.yaml").is_file(), f"litellm: profile file {profile}.yaml missing")

    docker = shutil.which("docker")
    if not docker or subprocess.run([docker, "compose", "version"], capture_output=True).returncode != 0:
        r.notes.append("docker compose not available: `docker compose config` NOT run (static checks only)")
        return
    for llm_profile in LLM_PROFILES:
        for compose_profiles in COMPOSE_PROFILES:
            args = [docker, "compose", "-f", str(COMPOSE_FILE), "--env-file", str(ROOT / ".env.example")]
            for p in compose_profiles:
                args += ["--profile", p]
            cmd_env = {**os.environ, "LLM_PROFILE": llm_profile}
            res = subprocess.run(args + ["config", "--format", "yaml"], capture_output=True, text=True, env=cmd_env)
            label = f"LLM_PROFILE={llm_profile}, Compose profiles={compose_profiles or 'none'}"
            if res.returncode != 0:
                r.errors.append(f"compose: `docker compose config` failed for {label}: {res.stderr.strip()}")
                continue
            resolved = yaml.safe_load(res.stdout)
            running = set(resolved["services"])
            expected = REQUIRED_SERVICES if compose_profiles else REQUIRED_SERVICES - {"ollama"}
            r.check(running == expected, f"compose: {label} resolves services {sorted(running)}")
            command = resolved["services"]["litellm"]["command"]
            r.check(f"/app/profiles/{llm_profile}.yaml" in command, f"compose: {label} does not load {llm_profile}.yaml")
            r.notes.append(f"docker compose config OK: {label}")


# --- 2. LiteLLM profiles ---------------------------------------------------------

OLLAMA_MODEL = re.compile(r"^ollama(_chat)?/[a-z0-9.\-]+:(?!latest$)[a-z0-9._\-]*\d+b[a-z0-9._\-]*$", re.I)
ANTHROPIC_MODEL = re.compile(r"^anthropic/claude-[a-z0-9\-]+-\d{8}$")


def check_litellm(r: Report) -> None:
    for profile in LLM_PROFILES:
        path = LITELLM_DIR / f"{profile}.yaml"
        if not path.is_file():
            continue
        cfg = load_yaml(path)
        names = [m.get("model_name") for m in cfg.get("model_list", [])]
        r.check(sorted(names) == sorted(ALIASES),
                f"litellm/{profile}: aliases are {sorted(names)}, expected exactly {sorted(ALIASES)} once each")
        for entry in cfg.get("model_list", []):
            params = entry.get("litellm_params", {})
            model = params.get("model", "")
            alias = entry.get("model_name")
            if model.startswith("ollama"):
                r.check(bool(OLLAMA_MODEL.match(model)), f"litellm/{profile}: {alias} model {model} is not a pinned Ollama tag")
                r.check(params.get("api_base") == "os.environ/OLLAMA_BASE_URL",
                        f"litellm/{profile}: {alias} must use api_base os.environ/OLLAMA_BASE_URL")
            elif model.startswith("anthropic/"):
                r.check(bool(ANTHROPIC_MODEL.match(model)), f"litellm/{profile}: {alias} model {model} is not a dated snapshot")
                r.check(params.get("api_key") == "os.environ/ANTHROPIC_API_KEY",
                        f"litellm/{profile}: {alias} must read os.environ/ANTHROPIC_API_KEY")
            else:
                r.errors.append(f"litellm/{profile}: {alias} uses an unknown provider ({model})")
        settings = cfg.get("litellm_settings") or {}
        for key in ("success_callback", "failure_callback", "callbacks"):
            value = settings.get(key) or []
            r.check(not value, f"litellm/{profile}: {key} must be empty (LiteLLM's own logging stays off), got {value}")
        # Parsed config only, so comments that explain why Langfuse is off are fine.
        r.check("langfuse" not in yaml.safe_dump(cfg).lower(), f"litellm/{profile}: must not configure Langfuse")
        master = (cfg.get("general_settings") or {}).get("master_key")
        r.check(master == "os.environ/LITELLM_MASTER_KEY", f"litellm/{profile}: master_key must come from the environment")


# --- 3. Knowledge base -----------------------------------------------------------

def contract_enums() -> dict[str, list[str]]:
    schemas = load_yaml(CONTRACT)["components"]["schemas"]
    return {name: schemas[name]["enum"] for name in REASON_ARTICLES}


def check_kb(r: Report) -> None:
    articles = {p.stem: p.read_text(encoding="utf-8") for p in sorted(KB_DIR.glob("*.md"))}
    r.check(10 <= len(articles) <= 15, f"kb: expected 10-15 articles, found {len(articles)}")

    for slug, text in articles.items():
        r.check(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug) is not None, f"kb: {slug}.md is not a lowercase slug")
        lines = text.splitlines()
        r.check(bool(lines) and lines[0].startswith("# "), f"kb: {slug}.md must start with a `# Title` line")
        r.check(sum(1 for line in lines if line.startswith("# ")) == 1, f"kb: {slug}.md must have exactly one H1")
        for pattern, what in INTERNAL_PATTERNS:
            hit = pattern.search(text)
            r.check(hit is None, f"kb: {slug}.md mentions {what} ({hit.group(0) if hit else ''})")
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if not re.match(r"https?://", link):
                r.check(link.endswith(".md") and link[:-3] in articles, f"kb: {slug}.md links to missing article {link}")

    for topic, slugs in KB_TOPICS.items():
        missing = [s for s in slugs if s not in articles]
        r.check(not missing, f"kb: topic '{topic}' has no article {missing}")

    for enum_name, values in contract_enums().items():
        mapping = REASON_ARTICLES[enum_name]
        unmapped = [v for v in values if v not in mapping]
        r.check(not unmapped, f"kb: {enum_name} values {unmapped} (banking contract) are not mapped to an article")
        for value, slug in mapping.items():
            r.check(value in values, f"kb: {enum_name}.{value} is mapped but no longer in the banking contract")
            r.check(slug in articles, f"kb: {enum_name}.{value} maps to missing article {slug}")


def main() -> int:
    failed = False
    for title, fn in (("compose", check_compose), ("litellm", check_litellm), ("knowledge base", check_kb)):
        r = Report()
        fn(r)
        for note in r.notes:
            print(f"  {note}")
        if r.errors:
            failed = True
            print(f"FAIL {title}")
            for e in r.errors:
                print(f"  - {e}")
        else:
            print(f"ok   {title}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
