[CmdletBinding()]
param(
    [string]$ServerHost = "10.101.64.13",
    [string]$ServerUser = "Administrator",
    [Parameter(Mandatory)]
    [string]$ServerPass,
    [Parameter(Mandatory)]
    [string]$OutputPath,
    [int]$IntervalSeconds = 5,
    [int]$DurationSeconds = 300
)

$ErrorActionPreference = "Stop"

# Build credential
$securePass = ConvertTo-SecureString $ServerPass -AsPlainText -Force
$credential = New-Object System.Management.Automation.PSCredential($ServerUser, $securePass)

# Open persistent PSSession
try {
    $session = New-PSSession -ComputerName $ServerHost -Credential $credential -ErrorAction Stop
}
catch {
    Write-Error "Failed to open PSSession to ${ServerHost}: $_"
    exit 1
}

# Write CSV header
$header = "timestamp,cpu_percent,ram_used_gb,ram_total_gb,net_recv_mbps,net_send_mbps"
try {
    Set-Content -Path $OutputPath -Value $header -Encoding UTF8
}
catch {
    Write-Error "Failed to write CSV header to ${OutputPath}: $_"
    Remove-PSSession $session
    exit 1
}

$startTime = [datetime]::UtcNow

try {
    while ($true) {
        $elapsed = ([datetime]::UtcNow - $startTime).TotalSeconds

        if ($DurationSeconds -gt 0 -and $elapsed -ge $DurationSeconds) {
            break
        }

        try {
            $metrics = Invoke-Command -Session $session -ScriptBlock {
                # CPU
                $cpuSample = (Get-Counter '\Processor(_Total)\% Processor Time' -ErrorAction Stop).CounterSamples
                $cpu = [math]::Round($cpuSample.CookedValue, 2)

                # RAM
                $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
                $ramTotalGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
                $ramFreeGB = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
                $ramUsedGB = [math]::Round($ramTotalGB - $ramFreeGB, 2)

                # Network — sum all adapters, bytes/sec -> Mbps
                $recvCounters = (Get-Counter '\Network Interface(*)\Bytes Received/sec' -ErrorAction Stop).CounterSamples
                $sentCounters = (Get-Counter '\Network Interface(*)\Bytes Sent/sec'     -ErrorAction Stop).CounterSamples
                $recvMbps = [math]::Round(($recvCounters | Measure-Object CookedValue -Sum).Sum / 125000, 3)
                $sentMbps = [math]::Round(($sentCounters | Measure-Object CookedValue -Sum).Sum / 125000, 3)

                [PSCustomObject]@{
                    CPU        = $cpu
                    RamUsedGB  = $ramUsedGB
                    RamTotalGB = $ramTotalGB
                    RecvMbps   = $recvMbps
                    SentMbps   = $sentMbps
                }
            }
        }
        catch {
            Write-Warning "Remote collection error: $_"
            Start-Sleep -Seconds $IntervalSeconds
            continue
        }

        $ts = [datetime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss")
        $row = "$ts,$($metrics.CPU),$($metrics.RamUsedGB),$($metrics.RamTotalGB),$($metrics.RecvMbps),$($metrics.SentMbps)"

        Add-Content -Path $OutputPath -Value $row -Encoding UTF8

        Start-Sleep -Seconds $IntervalSeconds
    }
}
finally {
    if ($session) {
        Remove-PSSession $session
    }
}

Write-Output "Collection complete."
