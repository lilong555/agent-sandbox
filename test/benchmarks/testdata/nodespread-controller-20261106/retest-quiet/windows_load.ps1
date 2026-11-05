param(
    [Parameter(Mandatory = $true)][string]$StopFile,
    [int]$IntervalSeconds = 5,
    [int]$MaxSamples = 1000
)

$ErrorActionPreference = "Stop"
$previous = @{}
$previousAt = [DateTimeOffset]::UtcNow
for ($index = 0; $index -lt $MaxSamples; $index++) {
    if (Test-Path -LiteralPath $StopFile) { break }
    try {
        $at = [DateTimeOffset]::UtcNow
        $cpu = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor |
            Where-Object Name -eq "_Total"
        $memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
        $current = @{}
        $elapsed = ($at - $previousAt).TotalSeconds
        $usage = @(Get-Process | ForEach-Object {
            if ($null -ne $_.CPU) {
                $key = [string]$_.Id
                $current[$key] = [double]$_.CPU
                if ($previous.ContainsKey($key) -and $elapsed -gt 0) {
                    $percent = 100 * ([double]$_.CPU - $previous[$key]) / $elapsed
                    if ($percent -gt 0) {
                        [ordered]@{
                            name = $_.ProcessName
                            pid = $_.Id
                            cpu_one_core_percent = [math]::Round($percent, 2)
                        }
                    }
                }
            }
        } | Sort-Object { $_.cpu_one_core_percent } -Descending |
            Select-Object -First 8)
        $previous = $current
        $previousAt = $at
        [ordered]@{
            utc = $at.ToString("o")
            cpu_percent = [int]$cpu.PercentProcessorTime
            available_mb = [int64]$memory.AvailableMBytes
            pages_input_per_sec = [int64]$memory.PagesInputPersec
            processes = $usage
        } | ConvertTo-Json -Depth 5 -Compress
    } catch {
        [ordered]@{
            utc = [DateTimeOffset]::UtcNow.ToString("o")
            error = $_.Exception.Message
        } | ConvertTo-Json -Compress
        exit 1
    }
    Start-Sleep -Seconds $IntervalSeconds
}
