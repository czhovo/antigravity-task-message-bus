[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

$srcDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$targetDir = Join-Path $HOME ".gemini\config\plugins\task-message-bus"

Write-Host "Installing task-message-bus plugin to: $targetDir"
New-Item -ItemType Directory -Path "$targetDir\scripts", "$targetDir\rules", "$targetDir\skills\task-message-bus" -Force | Out-Null

Copy-Item "$srcDir\plugin.json" "$targetDir\plugin.json" -Force
Copy-Item "$srcDir\hooks.json" "$targetDir\hooks.json" -Force
Copy-Item "$srcDir\README.md" "$targetDir\README.md" -Force
Copy-Item "$srcDir\rules\AGENTS.md" "$targetDir\rules\AGENTS.md" -Force
Copy-Item "$srcDir\skills\task-message-bus\SKILL.md" "$targetDir\skills\task-message-bus\SKILL.md" -Force
Copy-Item "$srcDir\scripts\*.py" "$targetDir\scripts\" -Force

$mcpServerPath = Join-Path $targetDir "scripts\mcp_server.py"
$mcpConfig = @{
    mcpServers = @{
        "task-message-bus" = @{
            command = "python"
            args    = @($mcpServerPath)
        }
    }
} | ConvertTo-Json -Depth 5

[System.IO.File]::WriteAllText((Join-Path $targetDir "mcp_config.json"), $mcpConfig + "`n", (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Installed successfully! Antigravity will auto-discover the plugin on the next turn."
