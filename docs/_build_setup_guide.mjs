/**
 * Regenerates docs/Org_Analyser_Setup_Guide.docx.
 * Run: node docs/_build_setup_guide.mjs
 */
import { writeFileSync } from "fs";
import {
  Document,
  Packer,
  Paragraph,
  TextRun,
  HeadingLevel,
  Table,
  TableRow,
  TableCell,
  WidthType,
  BorderStyle,
  ShadingType,
  LevelFormat,
} from "docx";

const PAGE_WIDTH = 12240;
const MARGIN = 1080;
const CONTENT = PAGE_WIDTH - MARGIN * 2;

const thin = { style: BorderStyle.SINGLE, size: 4, color: "CCCCCC" };
const borders = { top: thin, bottom: thin, left: thin, right: thin };

function p(text, opts = {}) {
  return new Paragraph({
    spacing: { after: 160 },
    ...opts,
    children: [
      new TextRun({
        text,
        font: "Calibri",
        size: opts.size || 22,
        bold: opts.bold || false,
        italics: opts.italics || false,
        color: opts.color,
      }),
    ],
  });
}

function note(text) {
  return new Paragraph({
    spacing: { before: 80, after: 160 },
    shading: { type: ShadingType.CLEAR, fill: "FFF8E1" },
    children: [
      new TextRun({ text: "NOTE: ", font: "Calibri", size: 20, bold: true }),
      new TextRun({ text, font: "Calibri", size: 20 }),
    ],
  });
}

function tip(text) {
  return new Paragraph({
    spacing: { before: 80, after: 160 },
    shading: { type: ShadingType.CLEAR, fill: "E8F5E9" },
    children: [
      new TextRun({ text: "TIP: ", font: "Calibri", size: 20, bold: true }),
      new TextRun({ text, font: "Calibri", size: 20 }),
    ],
  });
}

function important(text) {
  return new Paragraph({
    spacing: { before: 80, after: 160 },
    shading: { type: ShadingType.CLEAR, fill: "FFEBEE" },
    children: [
      new TextRun({ text: "IMPORTANT: ", font: "Calibri", size: 20, bold: true }),
      new TextRun({ text, font: "Calibri", size: 20 }),
    ],
  });
}

function cmd(text) {
  return new Paragraph({
    spacing: { before: 60, after: 120 },
    shading: { type: ShadingType.CLEAR, fill: "F5F5F5" },
    children: [
      new TextRun({ text, font: "Consolas", size: 18 }),
    ],
  });
}

function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 280, after: 160 },
    children: [new TextRun({ text, font: "Calibri", size: 32, bold: true })],
  });
}

function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 240, after: 120 },
    children: [new TextRun({ text, font: "Calibri", size: 26, bold: true })],
  });
}

function cell(text, opts = {}) {
  return new TableCell({
    borders,
    width: { size: opts.width || CONTENT / 2, type: WidthType.DXA },
    shading: opts.header
      ? { type: ShadingType.CLEAR, fill: "ECEFF1" }
      : undefined,
    children: [
      new Paragraph({
        spacing: { after: 40 },
        children: [
          new TextRun({
            text,
            font: "Calibri",
            size: 18,
            bold: !!opts.header || !!opts.bold,
          }),
        ],
      }),
    ],
  });
}

function twoColTable(rows, col1 = 2800, col2 = CONTENT - 2800) {
  return new Table({
    width: { size: CONTENT, type: WidthType.DXA },
    columnWidths: [col1, col2],
    rows: rows.map((r, i) =>
      new TableRow({
        children: [
          cell(r[0], { width: col1, header: i === 0, bold: i === 0 }),
          cell(r[1], { width: col2, header: i === 0 }),
        ],
      })
    ),
  });
}

const doc = new Document({
  numbering: {
    config: [
      {
        reference: "bullets",
        levels: [
          {
            level: 0,
            format: LevelFormat.BULLET,
            text: "•",
            alignment: "left",
            style: { paragraph: { indent: { left: 720, hanging: 360 } } },
          },
        ],
      },
    ],
  },
  sections: [
    {
      properties: {
        page: {
          size: { width: PAGE_WIDTH, height: 15840 },
          margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN },
        },
      },
      children: [
        new Paragraph({
          spacing: { after: 80 },
          children: [
            new TextRun({ text: "Org Analyser", font: "Calibri", size: 48, bold: true }),
          ],
        }),
        p("Step-by-Step Setup Guide", { size: 28, bold: true }),

        h1("What is this?"),
        p(
          "Org Analyser is a tool that automatically looks at all the code repositories (“repos” — think of them as project folders stored on GitHub or GitLab) belonging to a company or team, and produces reports about them: how many pull requests were merged, what kind of work was done, how good the code quality is, and more."
        ),
        p(
          "This guide walks through everything needed to get it running on your computer, one step at a time. You do not need to know how to write code — you just need to be able to copy and paste commands into a program called “Terminal.”"
        ),
        note(
          "Every gray box in this guide is a command. Copy it exactly (including any words in <angle brackets>, which you replace with your own values), paste it into Terminal, and press Enter."
        ),

        h1("Two ways to run (pick one)"),
        p(
          "After setup, you choose Mode 1 or Mode 2 depending on what you need:",
          { bold: true }
        ),
        twoColTable([
          ["Mode", "What it does"],
          [
            "1. Full pipeline (no task-profile)",
            "org-analyser run — clones every repo and runs merged-PR counts, profiler, eval-kit, and quality score. Does NOT run PR task-profile.",
          ],
          [
            "2. PR task-profile only",
            "org-analyser pr-task-profile  OR  pr-task-profile — classifies every merged pull request with rules + AI. No clones, no profiler, no eval-kit, no quality score.",
          ],
        ]),
        tip(
          "Both modes write under outputs/ on your machine. That whole folder is gitignored so client names, PR text, and scores are never committed by accident."
        ),

        h1("Step 1: Install the required tools (one-time setup)"),
        p(
          "Before running the pipeline, a few free tools need to be installed. This only has to be done once per computer."
        ),
        h2("If you are using a Mac"),
        p('1. Open the “Terminal” app (search for it with Spotlight: press Cmd + Space, type Terminal, press Enter).'),
        p('2. Install “Homebrew”, a tool that installs other tools. Paste this into Terminal and press Enter:'),
        cmd('/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'),
        p("3. Follow any on-screen instructions it gives you (it may ask for your Mac password — this is normal)."),
        p("4. Once Homebrew is installed, install the remaining tools:"),
        cmd("brew install git scc node"),
        h2("If you are using Windows"),
        p('1. Install “Chocolatey” by following the instructions at chocolatey.org/install (search “install Chocolatey Windows” if needed).'),
        p('2. Open “PowerShell” (search for it in the Start menu) and run:'),
        cmd("choco install git nodejs scc -y"),
        note(
          "If the scc install fails, download it manually from the scc releases page on GitHub, unzip it, and place scc.exe somewhere on your PATH. Ask a technical colleague for help with this step if needed."
        ),
        h2("Check everything installed correctly"),
        p("Paste each line below one at a time. Each should print a version number, not an error:"),
        cmd("git --version"),
        cmd("scc --version"),
        cmd("node --version"),
        cmd("npx --version"),

        h1("Step 2: Get the Org Pipeline code onto your computer"),
        p(
          "The code lives on GitHub at github.com/data927/org-analyser. To copy it to your computer, open Terminal, go to the folder where you want it to live, and run:"
        ),
        cmd("git clone https://github.com/data927/org-analyser.git"),
        p('This creates a new folder named “org-analyser”. Move into it:'),
        cmd("cd org-analyser"),
        note("From now on, every command in this guide should be run from inside this org-analyser folder."),

        h1("Step 3: Set up Python"),
        p(
          "This tool is written in Python, and it needs its own private, self-contained workspace (called a “virtual environment”) so it doesn’t interfere with anything else on your computer. Run these commands one line at a time:"
        ),
        cmd("python3.12 -m venv .venv"),
        cmd("source .venv/bin/activate"),
        cmd("python --version"),
        cmd("python -m pip install --upgrade pip"),
        cmd("pip install -e ."),
        cmd("cp config.example.yml config.yml"),
        p(
          "After running “source .venv/bin/activate”, you should see (.venv) appear at the start of your Terminal line. That means it worked."
        ),
        note(
          "The “python --version” command must show 3.10 or higher. If it doesn’t, try python3.11 or python3.10 instead of python3.12 in the first command above."
        ),
        important(
          "If you close Terminal and reopen it later, you must redo the “source .venv/bin/activate” line before running the tool again (on Windows: .venv\\Scripts\\activate). The install steps in Step 3 only need to be done once."
        ),

        h1("Step 4: Add your access keys (“tokens”)"),
        p(
          "The pipeline needs permission to read repositories and to use OpenAI for its analysis. These permissions come in the form of “tokens” — long secret passwords tied to your account. Never share these or post them publicly."
        ),
        p(
          "You already created your settings file, config.yml, back in Step 3. Open it in a text editor (TextEdit, VS Code, Notepad — anything)."
        ),
        p(
          "Find the section that begins with “tokens:” and fill in the line(s) for the platform you are using. Keep the two spaces before each name exactly as shown — YAML is fussy about spacing:"
        ),
        cmd("tokens:"),
        cmd("  github-data-token: ghp_your_github_token"),
        cmd("  gitlab_token: glpat_your_gitlab_token"),
        cmd("  openai_key: sk-your_openai_key"),
        cmd("  bitbucket_token: your_bitbucket_token"),
        cmd("  bitbucket_username: your_atlassian_email_or_bitbucket_username"),
        h2("Where to get each key"),
        twoColTable([
          ["Key", "How to get it / when you need it"],
          [
            "github-data-token",
            "Only needed if analyzing a GitHub org or repo. GitHub.com → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token. Tick the full “repo” scope. If the organisation uses SSO, also click Configure SSO → Authorize.",
          ],
          [
            "gitlab_token",
            "Only needed if analyzing a GitLab group or project. GitLab → User Settings → Access Tokens → create one with read_api / read_repository scope.",
          ],
          [
            "bitbucket_token",
            "Only for Bitbucket. API token (ATATT…): set bitbucket_username to your Atlassian login EMAIL. App password: set bitbucket_username to your Bitbucket USERNAME.",
          ],
          [
            "openai_key",
            "Always required for LLM steps (eval-kit + PR task-profile). Create at platform.openai.com/api-keys.",
          ],
        ]),
        note(
          "config.yml is deliberately excluded from git, so your secret keys are never accidentally uploaded anywhere. The entire outputs/ folder is also gitignored so client run results never land in the repo."
        ),

        h1("Step 5a: Mode 1 — full pipeline (everything except PR task-profile)"),
        p(
          "Pick the example below that matches what you want to analyze, replace the <placeholders>, and run it. Each run can take anywhere from minutes to several hours depending on how many repositories there are. This mode does not run PR task-profile — use Mode 2 for that."
        ),
        p("Analyze an entire GitHub organization", { bold: true }),
        cmd("org-analyser --github-org <ORG_NAME> --workers 10"),
        p("Analyze a single GitHub repository", { bold: true }),
        cmd("org-analyser --github-repo <OWNER>/<REPO> --workers 1"),
        p("Analyze an entire GitLab group", { bold: true }),
        cmd("org-analyser --gitlab-group <GROUP_NAME> --workers 10"),
        p("Analyze a single GitLab project", { bold: true }),
        cmd("org-analyser --gitlab-project <GROUP>/<PROJECT> --workers 1"),
        p("Analyze a Bitbucket workspace or repo", { bold: true }),
        cmd("org-analyser --bitbucket-workspace <WORKSPACE> --workers 10"),
        cmd("org-analyser --bitbucket-repo <WORKSPACE>/<REPO> --workers 1"),
        p("Analyze a folder of repos already on your computer", { bold: true }),
        cmd("org-analyser --local-repos-dir ./repos --workers 4"),
        tip(
          "Mode 1 already skips PR task-profile. To skip sealed quality scores as well, add --skip-quality-score. Only pass --no-skip-pr-task-profile if you deliberately want Mode 1 and Mode 2 glued into one run."
        ),
        note(
          "If your terminal says “org-analyser: command not found”, make sure .venv is active (Step 3), or use “python cli.py” with the same flags instead."
        ),

        h1("Step 5b: Mode 2 — PR task-profile only"),
        p(
          "Use this when you only need rules + LLM classification of every merged pull request. It does not clone repos and does not run profiler / eval-kit / quality score."
        ),
        p("Either command works (they are the same tool):", { bold: true }),
        cmd("org-analyser pr-task-profile --org <ORG_NAME>"),
        cmd("pr-task-profile --org <ORG_NAME>"),
        p("Useful knobs for large orgs (keeps the run from looking “stuck”):", { bold: true }),
        cmd("pr-task-profile --org <ORG_NAME> --max-prs 200 --repo-timeout 1800 --org-timeout 14400"),
        cmd("pr-task-profile --repo owner/a,owner/b --llm-mode auto"),
        cmd("pr-task-profile --help"),
        twoColTable([
          ["Flag", "Meaning (defaults are hang-proof)"],
          ["--max-prs N", "Only classify the newest N merged PRs per repo (smoke runs)."],
          ["--repo-timeout 1800", "Give up on one slow repo after 30 minutes; continue with the rest."],
          ["--org-timeout 14400", "Stop starting new repos after 4 hours; flush a partial summary."],
          ["--batch-poll-timeout 7200", "Wait at most 2 hours for an OpenAI Batch job; never stampede live sync calls afterward."],
        ]),
        note(
          "Mode 2 still needs GITHUB_TOKEN / GITLAB_TOKEN / BITBUCKET_* (via config.yml → env) plus an OpenAI/Azure key. Output lands under outputs/…/scan_<timestamp>/ (org_summary.csv/json + per-repo files)."
        ),

        h1("Step 5c: Analyzing code already saved on your computer"),
        p(
          "If the projects you want to look at are already on your computer, you can analyze them with Mode 1 directly. You do not need a GitHub or GitLab account or token for the code-based phases."
        ),
        cmd('org-analyser --local-repos-dir "/path/to/your-project" --local-only'),
        cmd('org-analyser --local-repos-dir "/path/to/folder-of-projects" --local-only'),
        note(
          "“--local-only” keeps the whole run on your computer. It skips the Pull Request parts of the report (those live online) and will not contact GitHub or GitLab at all. Mode 2 (PR task-profile) always needs API access to the hosting platform."
        ),

        h1("Step 6: Find your results"),
        p("When the run finishes, look inside the org-analyser folder for:"),
        cmd("outputs/org-analyser-runs/"),
        p(
          "Inside, there will be a new folder named after your org/repo and the date/time of the run. It contains spreadsheets (.xlsx/.csv), data files (.json), logs, and a .zip file with everything bundled together — that zip is the file to share with others."
        ),
        p(
          "For Mode 2 alone, look under the --output-dir you passed (default outputs/) for scan_<timestamp>/org_summary.csv."
        ),
        important(
          "Never commit the outputs/ folder. It can contain client org names, PR titles/bodies, and quality scores. It is already listed in .gitignore."
        ),

        h1("Troubleshooting"),
        twoColTable(
          [
            ["Problem", "What to try"],
            [
              "The pipeline stops immediately after starting",
              "Check that you filled in openai_key in config.yml, and the correct GitHub/GitLab token for the platform you're analyzing. Run: org-analyser check --github-org <ORG>",
            ],
            [
              "PR task-profile looks stuck",
              "It runs last and can take hours on large orgs, but progress is streamed. Watch logs/pr-task-profile.stream.log or use Mode 2 with --max-prs / --repo-timeout. Rate-limit sleeps are capped; Batch waits are capped at 2h.",
            ],
            [
              "“command not found: python3.12”",
              "Try python3.11 or python3.10 instead, wherever python3.12 appears in Step 3.",
            ],
            [
              "Cloning a repo fails",
              "Double check your github-data-token or gitlab_token has access to that repository, and that you typed the org/repo name correctly.",
            ],
            [
              "Some repos fail but others succeed",
              "This is expected on large organizations. The run continues automatically — check manifest.json (Mode 1) or failures.json (Mode 2).",
            ],
            [
              "Private GitHub repositories are missing",
              "Use a CLASSIC token with the full “repo” scope, put it in github-data-token, and authorize SSO if the org requires it.",
            ],
          ],
          3200,
          CONTENT - 3200
        ),

        h1("Glossary (plain-language definitions)"),
        twoColTable([
          ["Term", "Meaning"],
          ["Terminal", "A text-based program on your computer where you type commands instead of clicking buttons."],
          ["Repo (repository)", "A project's folder of code, stored online on GitHub or GitLab."],
          ["Org (organization)", "A group of repos owned by one company or team on GitHub/GitLab."],
          ["Clone", "Making a copy of an online repo onto your own computer."],
          ["Token", "A secret password that proves you have permission to access something, like a repo or an AI service."],
          ["Virtual environment (venv)", "A private, isolated space for Python so this tool's software doesn't conflict with anything else installed on your computer."],
          ["Pull Request (PR)", "A proposed change to a codebase that a developer submits for review before it's added to the project."],
          ["PR task-profile", "The report that labels each merged PR as simple_fix / standard_feature_work / rich_task / other / automated (rules + LLM)."],
        ]),
      ],
    },
  ],
});

const out = new URL("./Org_Analyser_Setup_Guide.docx", import.meta.url);
const buf = await Packer.toBuffer(doc);
writeFileSync(out, buf);
console.log("wrote", out.pathname);
