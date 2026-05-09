class ClaudeSessionBudget < Formula
  desc "Track Claude Code's 5-hour session usage and pause before hitting the limit"
  homepage "https://github.com/Star001-KR/claude-session-budget"
  url "https://github.com/Star001-KR/claude-session-budget/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "89eec96e9aac9ab63e80f347d9d22f90cd1f7c702af8891c218bde0bf58333bd"
  license "MIT"

  depends_on "python@3.13"

  def install
    libexec.install Dir["scripts/*.py"]

    python = Formula["python@3.13"].opt_bin/"python3.13"

    {
      "budget-check"     => "budget_check.py",
      "budget-calibrate" => "calibrate.py",
    }.each do |cmd, script|
      (bin/cmd).write <<~BASH
        #!/bin/bash
        exec "#{python}" "#{libexec}/#{script}" "$@"
      BASH
      chmod 0755, bin/cmd
    end

    pkgshare.install "skills", ".claude-plugin", "hooks", ".env.example"
    doc.install "README.md", "docs"
  end

  def caveats
    <<~EOS
      To use as a Claude Code PreToolUse hook, add to ~/.claude/settings.json:

        "hooks": {
          "PreToolUse": [
            {
              "matcher": "*",
              "hooks": [{
                "type": "command",
                "command": "#{HOMEBREW_PREFIX}/bin/budget-check"
              }]
            }
          ]
        }

      Or install as a Claude Code plugin (recommended):
        /plugin marketplace add Star001-KR/claude-session-budget
        /plugin install session-budget
    EOS
  end

  test do
    output = shell_output("#{bin}/budget-check 2>&1")
    assert_match "session-budget", output
  end
end
