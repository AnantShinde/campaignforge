#!/usr/bin/env python3
"""
CampaignForge CLI
-----------------
Interactive terminal input for campaign brief submission.

Usage:
    python cli.py
    python cli.py --profile campaignforge --env dev
"""
import argparse
import json
import pathlib
import ssl
import sys
import time
import urllib.request

import boto3
import questionary
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich import box

console = Console()

# ── Country → primary language ────────────────────────────────────────────────
# Maps lowercase country name / common aliases to BCP-47 language code.
# English-primary countries are tagged "en" so we don't offer a duplicate.
COUNTRY_LANG = {
    # English-primary — official/primary language IS English, no duplicate offered
    "united states": "en", "usa": "en", "us": "en",
    "united kingdom": "en", "uk": "en", "england": "en",
    "australia": "en", "new zealand": "en", "canada": "en",
    "ireland": "en", "singapore": "en",
    "south africa": "en", "south afrika": "en", "s. africa": "en",  # common typo/alias
    "nigeria": "en", "kenya": "en", "ghana": "en",
    # Non-English primary
    "brazil": "pt-BR", "brasil": "pt-BR",
    "portugal": "pt",
    "mexico": "es", "méxico": "es",
    "spain": "es", "españa": "es",
    "argentina": "es", "colombia": "es", "chile": "es",
    "peru": "es", "venezuela": "es",
    "germany": "de", "deutschland": "de",
    "austria": "de", "switzerland": "de",
    "france": "fr",
    "italy": "it", "italia": "it",
    "japan": "ja", "日本": "ja",
    "south korea": "ko", "korea": "ko", "한국": "ko",
    "china": "zh", "中国": "zh",
    "taiwan": "zh-TW",
    "india": "hi",  # Hindi is official; English widely used in business
    "netherlands": "nl", "holland": "nl",
    "russia": "ru",
    "saudi arabia": "ar", "uae": "ar",
    "united arab emirates": "ar",
    "turkey": "tr", "türkiye": "tr",
    "indonesia": "id",
    "thailand": "th",
    "vietnam": "vi",
    "poland": "pl",
    "sweden": "sv",
    "norway": "no",
    "denmark": "da",
    "finland": "fi",
    "czech republic": "cs",
    "hungary": "hu",
}

LANG_LABELS = {
    "en": "English", "pt-BR": "Portuguese (Brazil)", "pt": "Portuguese",
    "es": "Spanish", "de": "German", "fr": "French", "it": "Italian",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese (Simplified)",
    "zh-TW": "Chinese (Traditional)", "hi": "Hindi", "nl": "Dutch",
    "ru": "Russian", "ar": "Arabic", "tr": "Turkish", "id": "Indonesian",
    "th": "Thai", "vi": "Vietnamese", "pl": "Polish", "sv": "Swedish",
    "no": "Norwegian", "da": "Danish", "fi": "Finnish", "cs": "Czech",
    "hu": "Hungarian",
}

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE


# ── Auth ──────────────────────────────────────────────────────────────────────
def get_jwt(profile: str, env: str) -> tuple[str, str]:
    session = boto3.Session(profile_name=profile, region_name="us-east-1")
    cf = session.client("cloudformation")

    def output(key_fragment):
        stk = cf.describe_stacks(StackName=f"CampaignForge-{env}-Api")["Stacks"][0]["Outputs"]
        return next(o["OutputValue"] for o in stk if key_fragment.lower() in o["OutputKey"].lower())

    api_url   = output("ApiUrl").rstrip("/")
    pool_id   = output("UserPoolId")
    client_id = output("WebClientId")

    idp = session.client("cognito-idp")
    try:
        idp.admin_create_user(
            UserPoolId=pool_id, Username="cli@campaignforge.internal",
            TemporaryPassword="CfCli@2025!", MessageAction="SUPPRESS"
        )
        idp.admin_set_user_password(
            UserPoolId=pool_id, Username="cli@campaignforge.internal",
            Password="CfCli@2025!", Permanent=True
        )
    except Exception:
        pass

    auth = idp.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=client_id,
        AuthParameters={"USERNAME": "cli@campaignforge.internal", "PASSWORD": "CfCli@2025!"}
    )
    return auth["AuthenticationResult"]["IdToken"], api_url


# ── API calls ─────────────────────────────────────────────────────────────────
def _api(method: str, path: str, api_url: str, jwt: str, body=None):
    req = urllib.request.Request(
        f"{api_url}{path}",
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
        method=method
    )
    with urllib.request.urlopen(req, context=ssl_ctx) as r:
        return json.loads(r.read())


def submit_single(api_url, jwt, brief) -> str:
    return _api("POST", "/brief", api_url, jwt, brief)["campaign_id"]


def submit_batch(api_url, jwt, briefs) -> list[str]:
    return _api("POST", "/brief/batch", api_url, jwt, {"briefs": briefs})["campaign_ids"]


def poll(api_url, jwt, campaign_id) -> dict:
    data = _api("GET", f"/campaigns/{campaign_id}", api_url, jwt)
    bps  = data.get("blueprints", [data])
    return bps[0] if bps else {}


# ── Display ───────────────────────────────────────────────────────────────────
def show_result(bp: dict, label: str = ""):
    p1       = bp.get("compliance_pass1", {}).get("overall", "—")
    p2       = bp.get("compliance_pass2", {}).get("overall", "—")
    approval = bp.get("approval_status", "—")
    copies   = bp.get("ad_copy", [{}])
    copy     = copies[0] if copies else {}

    def col(v): return "green" if v == "pass" else "yellow" if v == "warn" else "red"

    title = f"[bold cyan]Result{' — ' + label if label else ''}[/bold cyan]"
    console.print(Panel(
        f"[bold]Campaign ID [/bold]  {bp.get('campaign_id','—')}\n"
        f"[bold]Approval    [/bold]  {approval}\n"
        f"[bold]Compliance  [/bold]  P1: [{col(p1)}]{p1}[/{col(p1)}]   "
        f"P2: [{col(p2)}]{p2}[/{col(p2)}]",
        title=title, border_style="cyan"
    ))

    if copy.get("headline"):
        t = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0, 1))
        t.add_column(style="bold dim", width=14)
        t.add_column()
        t.add_row("Headline",    copy.get("headline", ""))
        t.add_row("Subheadline", copy.get("subheadline", ""))
        t.add_row("Body",        copy.get("body", ""))
        t.add_row("CTA",         copy.get("cta", ""))
        console.print(t)

    images = bp.get("images", {})
    if images:
        campaign_id = bp.get("campaign_id", "unknown")
        out_dir = pathlib.Path("results") / campaign_id
        out_dir.mkdir(parents=True, exist_ok=True)
        console.print("[bold]Images:[/bold]")
        for ratio, meta in sorted(images.items()):
            url  = meta.get("url", "") if isinstance(meta, dict) else ""
            plat = meta.get("platform", ratio) if isinstance(meta, dict) else ratio
            if url:
                dest = out_dir / f"{ratio}.png"
                with urllib.request.urlopen(url, context=ssl_ctx) as resp:
                    dest.write_bytes(resp.read())
                console.print(f"  [green]✓[/green]  {ratio} ({plat})  →  {dest}")


def wait_for(api_url, jwt, campaign_id, label="") -> dict:
    """Polls until complete, shows a live spinner. Returns blueprint."""
    start = time.time()
    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  console=console, transient=True) as progress:
        task = progress.add_task("", total=None)
        while True:
            elapsed = int(time.time() - start)
            if elapsed > 300:
                console.print("[red]Timed out.[/red]"); sys.exit(1)

            bp     = poll(api_url, jwt, campaign_id)
            status = bp.get("status", "queued")
            desc   = f"Generating{' (' + label + ')' if label else ''}...  [{elapsed}s]  {status}"
            progress.update(task, description=desc)

            if status == "complete":
                break
            elif status == "failed":
                console.print(f"[red]Failed:[/red] {bp.get('failure_reason','unknown')}"); sys.exit(1)
            time.sleep(3)

    elapsed = int(time.time() - start)
    console.print(f"[green]✓[/green] Complete in [bold]{elapsed}s[/bold]{' (' + label + ')' if label else ''}")
    return bp


# ── Main flow ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="CampaignForge CLI")
    parser.add_argument("--profile", default="campaignforge")
    parser.add_argument("--env",     default="dev")
    args = parser.parse_args()

    console.print(Panel(
        "[bold white]CampaignForge[/bold white]  [dim]Automated Social Ad Campaign Generator[/dim]",
        border_style="bright_blue"
    ))
    console.print()

    # ── Product ──────────────────────────────────────────────────────────────
    console.rule("[bold]Product", align="left")

    product_name = questionary.text(
        "Product name:",
        validate=lambda v: True if v.strip() else "Required"
    ).ask()
    if not product_name: sys.exit(0)

    product_details = questionary.text(
        "Product details  (key features / USP — optional):",
        instruction="Press Enter to skip"
    ).ask() or ""

    # ── Audience ─────────────────────────────────────────────────────────────
    console.rule("[bold]Target Audience", align="left")

    audience = questionary.text(
        "Who is this for?",
        default="professionals aged 25-45",
        validate=lambda v: True if v.strip() else "Required"
    ).ask()
    if not audience: sys.exit(0)

    # ── Country ──────────────────────────────────────────────────────────────
    console.rule("[bold]Countries & Language", align="left")

    countries_raw = questionary.text(
        "Target countries  (separate multiple with commas):",
        instruction="e.g.  Brazil, Germany, Japan",
        validate=lambda v: True if v.strip() else "Required"
    ).ask()
    if not countries_raw: sys.exit(0)

    # Parse comma-separated list and detect language per country
    country_list = [c.strip() for c in countries_raw.split(",") if c.strip()]
    parsed = []
    for c in country_list:
        lang = COUNTRY_LANG.get(c.lower(), "en")
        parsed.append({
            "country":  c,
            "region":   c.lower(),
            "lang":     lang,
            "label":    LANG_LABELS.get(lang, lang),
            "is_en":    lang == "en",
        })

    # Show detected languages
    console.print()
    t_det = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    t_det.add_column("Country", style="bold")
    t_det.add_column("Detected official language")
    for p in parsed:
        tag = " [dim](English primary)[/dim]" if p["is_en"] else ""
        t_det.add_row(p["country"], p["label"] + tag)
    console.print(t_det)
    console.print()

    # ── Language preference ───────────────────────────────────────────────────
    has_non_english = any(not p["is_en"] for p in parsed)

    if not has_non_english:
        # All countries are English-primary — no question needed
        console.print("[dim]All countries are English-primary — generating in English.[/dim]")
        lang_choice = "english"
    else:
        lang_choice = questionary.select(
            "Generate copy in:",
            choices=[
                questionary.Choice(
                    "English only  (all countries)",
                    value="english"
                ),
                questionary.Choice(
                    "Each country's official language  (English countries stay English)",
                    value="official"
                ),
                questionary.Choice(
                    "Both  (English + official language per country — skips English duplicate)",
                    value="both"
                ),
            ]
        ).ask()
        if not lang_choice: sys.exit(0)

    # Build the (country, language_code, label) pairs to generate
    languages = []  # list of (parsed_country_dict, lang_code, lang_label)
    for p in parsed:
        if lang_choice == "english":
            languages.append((p, "en", "English"))
        elif lang_choice == "official":
            languages.append((p, p["lang"], p["label"]))
        else:  # both
            languages.append((p, "en", "English"))
            if not p["is_en"]:  # only add official if it's not English
                languages.append((p, p["lang"], p["label"]))

    # ── Message ───────────────────────────────────────────────────────────────
    console.rule("[bold]Core Message", align="left")

    message = questionary.text(
        "Core campaign message:",
        default="Quality that speaks for itself",
        validate=lambda v: True if v.strip() else "Required"
    ).ask()
    if not message: sys.exit(0)

    full_audience = audience + (f" — {product_details}" if product_details else "")

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print()
    t = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
    t.add_column(style="bold dim", width=16)
    t.add_column()
    t.add_row("Product",   product_name)
    t.add_row("Audience",  full_audience[:80])
    t.add_row("Countries", countries_raw.strip())
    t.add_row("Message",   message)
    t.add_row("Campaigns", f"{len(languages)} brief(s) to generate")
    for p, lc, ll in languages:
        t.add_row("", f"• {p['country']} — {ll}")
    console.print(Panel(t, title="[bold]Brief Summary[/bold]", border_style="white"))

    if not questionary.confirm("Submit?", default=True).ask():
        console.print("[dim]Cancelled.[/dim]"); sys.exit(0)

    # ── Submit ────────────────────────────────────────────────────────────────
    console.print()
    with console.status("[bold cyan]Connecting...[/bold cyan]"):
        jwt, api_url = get_jwt(args.profile, args.env)

    briefs = [
        {
            "product_name": product_name,
            "region":       p["region"],
            "audience":     full_audience,
            "message":      message,
            "language":     lc,
        }
        for p, lc, ll in languages
    ]

    with console.status("[bold cyan]Submitting...[/bold cyan]"):
        if len(briefs) == 1:
            ids = [submit_single(api_url, jwt, briefs[0])]
        else:
            ids = submit_batch(api_url, jwt, briefs)

    for cid, (p, lc, ll) in zip(ids, languages):
        console.print(f"[green]✓[/green] {p['country']} ({ll}): [bold]{cid}[/bold]")

    # ── Poll & display ────────────────────────────────────────────────────────
    console.print()
    for cid, (p, lc, ll) in zip(ids, languages):
        label = f"{p['country']} — {ll}" if len(ids) > 1 else ""
        bp = wait_for(api_url, jwt, cid, label=label)
        show_result(bp, label=label)
        console.print()


if __name__ == "__main__":
    main()
