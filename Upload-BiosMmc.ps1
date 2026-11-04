#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SourceDirectory = 'C:\LocalPath\Bios',
    [string]$BiosArchive,
    [string]$MmcArchive
)

$ErrorActionPreference = 'Stop'

function Get-UploadArchives {
    param([string]$SourceDirectory, [string]$BiosArchive, [string]$MmcArchive)

    $biosPattern = '^\d{4}\.D\d+\.7z$'
    $mmcPattern = '^MMCFW_\d+\.\d+\.\d+\.7z$'
    if ([bool]$BiosArchive -ne [bool]$MmcArchive) {
        throw 'Specify both -BiosArchive and -MmcArchive, or neither.'
    }
    if ($BiosArchive) {
        $bios = Get-Item -LiteralPath $BiosArchive -ErrorAction Stop
        $mmc = Get-Item -LiteralPath $MmcArchive -ErrorAction Stop
    } else {
        $files = @(Get-ChildItem -LiteralPath $SourceDirectory -File -ErrorAction Stop)
        $biosFiles = @($files | Where-Object { $_.Name -cmatch $biosPattern })
        $mmcFiles = @($files | Where-Object { $_.Name -cmatch $mmcPattern })
        if ($biosFiles.Count -ne 1 -or $mmcFiles.Count -ne 1) {
            throw "Expected exactly one BIOS and one MMC archive in $SourceDirectory. Use -BiosArchive and -MmcArchive to select an explicit pair."
        }
        $bios = $biosFiles[0]
        $mmc = $mmcFiles[0]
    }
    if ($bios.PSIsContainer -or $bios.Name -cnotmatch $biosPattern) {
        throw 'BIOS archive name must be <four-digit release>.D<revision>.7z.'
    }
    if ($mmc.PSIsContainer -or $mmc.Name -cnotmatch $mmcPattern) {
        throw 'MMC archive name must be MMCFW_<major>.<minor>.<patch>.7z.'
    }
    return @($bios, $mmc)
}

function Confirm-ArtifactoryUpload {
    param([object[]]$Plans)

    if (-not [Environment]::UserInteractive) {
        throw 'Upload confirmation requires an interactive Windows desktop. Use --no-upload to package without uploading.'
    }
    Add-Type -AssemblyName System.Windows.Forms
    $details = ($Plans | ForEach-Object {
        "{0}: {1}`n{2:N0} bytes`nTo: {3}" -f $_.Kind, $_.File.FullName, $_.Length, $_.UploadUri
    }) -join "`n`n"
    $answer = [System.Windows.Forms.MessageBox]::Show(
        "Upload these BIOS and MMC archives?`n`n$details`n`nYes: enter one token and upload both. No: keep the local archives only.",
        'Confirm BIOS/MMC upload',
        [System.Windows.Forms.MessageBoxButtons]::YesNoCancel,
        [System.Windows.Forms.MessageBoxIcon]::Question,
        [System.Windows.Forms.MessageBoxDefaultButton]::Button2,
        [System.Windows.Forms.MessageBoxOptions]::DefaultDesktopOnly
    )
    return $answer -eq [System.Windows.Forms.DialogResult]::Yes
}

function Read-ArtifactoryToken {
    $credential = Get-Credential -UserName 'ArtifactoryAccessToken' -Message (
        'Paste your Artifactory Access Token into the Password field. ' +
        'Only this BIOS/MMC upload will use it; it will not be saved.'
    )
    if ($null -ne $credential) {
        return $credential.Password
    }
}

function Invoke-ArtifactoryRequest {
    param(
        [string]$Uri,
        [string]$AuthConfig,
        [System.IO.FileInfo]$File,
        [string]$Sha256
    )

    $curl = (Get-Command curl.exe -CommandType Application -ErrorAction Stop).Source
    $arguments = @(
        '--disable', '--globoff', '--config', '-', '--silent', '--show-error',
        '--connect-timeout', '20', '--max-time', '60',
        '--write-out', "`n__ARTIFACTORY_RESULT__=%{http_code};%{size_upload};%{speed_upload};%{time_total}`n",
        '--url', $Uri
    )
    if ($null -ne $File) {
        $arguments += @(
            '--max-time', '1800', '--header', 'Content-Type: application/octet-stream',
            '--header', "X-Checksum-Sha256: $Sha256", '--upload-file', $File.FullName
        )
    }
    # Keep the token out of process arguments, files and curl's default configuration.
    $lines = @($AuthConfig | & $curl @arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "curl failed (exit $LASTEXITCODE) for $Uri."
    }
    $text = $lines -join "`n"
    $matches = [regex]::Matches(
        $text, '(?m)^__ARTIFACTORY_RESULT__=(\d{3});([\d.]+);([\d.]+);([\d.]+)\r?$'
    )
    if ($matches.Count -eq 0) {
        throw "Missing HTTP status in curl response for $Uri."
    }
    $result = $matches[$matches.Count - 1]
    return [pscustomobject]@{
        Status = [int]$result.Groups[1].Value
        Body = $text.Substring(0, $result.Index).Trim()
        UploadedBytes = $result.Groups[2].Value
        BytesPerSecond = $result.Groups[3].Value
        Seconds = $result.Groups[4].Value
    }
}

function Assert-RemoteArchive {
    param($Response, $Plan)

    if ($Response.Status -ne 200) {
        throw "Cannot verify $($Plan.Kind) archive (HTTP $($Response.Status)): $($Plan.UploadUri)"
    }
    $metadata = $Response.Body | ConvertFrom-Json -ErrorAction Stop
    if ($metadata.repo -cne 'DEG-IFWI-LOCAL' -or $metadata.path -cne "/$($Plan.RemotePath)") {
        throw "Unexpected repository or path in $($Plan.Kind) metadata."
    }
    if (
        [string]$metadata.size -cne [string]$Plan.Length -or
        [string]$metadata.checksums.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or
        [string]$metadata.checksums.sha256 -ne $Plan.Sha256
    ) {
        throw "Remote $($Plan.Kind) size or SHA-256 differs from the local archive. Refusing to overwrite or report success: $($Plan.UploadUri)"
    }
}

function Invoke-BiosMmcUpload {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [Parameter(Mandatory = $true)][string]$SourceDirectory,
        [string]$BiosArchive,
        [string]$MmcArchive
    )

    $VerbosePreference = 'SilentlyContinue'
    $DebugPreference = 'SilentlyContinue'
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $root = 'https://af01p-sc.devtools.intel.com/artifactory'
    $repository = 'DEG-IFWI-LOCAL'
    $folders = @('FIV_Portal/IFWI_CI/BIOSID', 'FIV_Portal/IFWI_CI/MMC')
    $kinds = @('BIOS', 'MMC')
    $plans = @()
    $secureToken = $null
    $token = $null
    $authConfig = $null
    try {
        $files = @(Get-UploadArchives $SourceDirectory $BiosArchive $MmcArchive)
        for ($index = 0; $index -lt $files.Count; $index++) {
            $remotePath = "$($folders[$index])/$($files[$index].Name)"
            $plan = [pscustomobject]@{
                Kind = $kinds[$index]
                File = $files[$index]
                RemotePath = $remotePath
                UploadUri = "$root/$repository/$remotePath"
                MetadataUri = "$root/api/storage/$repository/$remotePath"
                FolderUri = "$root/api/storage/$repository/$($folders[$index])"
                FolderPath = "/$($folders[$index])"
                Stream = $null
                Length = 0L
                Sha256 = ''
                Exists = $false
            }
            $plans += $plan
            $plan.Stream = [System.IO.File]::Open(
                $plan.File.FullName, [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read
            )
            $plan.Length = $plan.Stream.Length
            if ($plan.Length -le 0) {
                throw "Archive is empty: $($plan.File.FullName)"
            }
            $hasher = [System.Security.Cryptography.SHA256]::Create()
            try {
                $plan.Sha256 = [BitConverter]::ToString(
                    $hasher.ComputeHash($plan.Stream)
                ).Replace('-', '')
            } finally {
                $hasher.Dispose()
            }
            Write-Output "UPLOAD_PLAN $($plan.Kind): $($plan.File.FullName) -> $($plan.UploadUri); bytes=$($plan.Length); SHA256=$($plan.Sha256)"
        }
        if (-not $PSCmdlet.ShouldProcess(
            ($plans.UploadUri -join '; '), 'Upload BIOS and MMC archives after confirmation'
        )) {
            if ($WhatIfPreference) {
                Write-Output 'UPLOAD_RESULT=preview'
            } else {
                Write-Output 'Upload cancelled; local archives retained.'
                Write-Output 'UPLOAD_RESULT=cancelled'
            }
            return
        }
        if (-not (Confirm-ArtifactoryUpload $plans)) {
            Write-Output 'Upload declined; no token requested, no HTTP requests made, local archives retained.'
            Write-Output 'UPLOAD_RESULT=cancelled'
            return
        }
        $secureToken = Read-ArtifactoryToken
        if ($null -eq $secureToken) {
            Write-Output 'Token entry cancelled; no HTTP requests made, local archives retained.'
            Write-Output 'UPLOAD_RESULT=cancelled'
            return
        }
        $address = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        try {
            $token = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($address).Trim()
        } finally {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($address)
        }
        if ($token -cnotmatch '^[A-Za-z0-9._~+/-]+=*$') {
            throw 'Invalid token format. Enter only the access token, without a Bearer prefix.'
        }
        $authConfig = 'header = "Authorization: Bearer ' + $token + '"'
        foreach ($plan in $plans) {
            $folder = Invoke-ArtifactoryRequest -Uri $plan.FolderUri -AuthConfig $authConfig
            if ($folder.Status -ne 200) {
                throw "Cannot access the $($plan.Kind) destination folder (HTTP $($folder.Status))."
            }
            $metadata = $folder.Body | ConvertFrom-Json -ErrorAction Stop
            if ($metadata.repo -cne $repository -or $metadata.path.TrimEnd('/') -cne $plan.FolderPath) {
                throw "Unexpected $($plan.Kind) destination folder metadata."
            }
            $existing = Invoke-ArtifactoryRequest -Uri $plan.MetadataUri -AuthConfig $authConfig
            if ($existing.Status -eq 200) {
                Assert-RemoteArchive $existing $plan
                $plan.Exists = $true
            } elseif ($existing.Status -ne 404) {
                throw "Cannot check existing $($plan.Kind) archive (HTTP $($existing.Status)); no uploads started."
            }
        }
        $uploaded = 0
        $skipped = 0
        foreach ($plan in $plans) {
            if ($plan.Exists) {
                Write-Output "SKIPPED $($plan.Kind): identical size and SHA-256 already present: $($plan.UploadUri)"
                $skipped++
                continue
            }
            Write-Output "UPLOADING $($plan.Kind): $($plan.Length) bytes -> $($plan.UploadUri)"
            $response = Invoke-ArtifactoryRequest -Uri $plan.UploadUri -AuthConfig $authConfig -File $plan.File -Sha256 $plan.Sha256
            if ($response.Status -ne 200 -and $response.Status -ne 201) {
                throw "$($plan.Kind) upload rejected (HTTP $($response.Status)); local archives retained."
            }
            if ([decimal]$response.UploadedBytes -ne $plan.Length) {
                throw "$($plan.Kind) upload sent an unexpected number of bytes."
            }
            $remote = Invoke-ArtifactoryRequest -Uri $plan.MetadataUri -AuthConfig $authConfig
            Assert-RemoteArchive $remote $plan
            Write-Output "VERIFIED $($plan.Kind): $($plan.Length) bytes; SHA256=$($plan.Sha256); seconds=$($response.Seconds); bytes/s=$($response.BytesPerSecond)"
            $uploaded++
        }
        Write-Output "Upload summary: uploaded=$uploaded, identical/skipped=$skipped, verified=$($plans.Count)."
        Write-Output 'UPLOAD_RESULT=completed'
    } finally {
        foreach ($plan in $plans) {
            if ($null -ne $plan.Stream) {
                $plan.Stream.Dispose()
            }
        }
        $authConfig = $null
        $token = $null
        if ($null -ne $secureToken) {
            $secureToken.Dispose()
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-BiosMmcUpload -SourceDirectory $SourceDirectory -BiosArchive $BiosArchive -MmcArchive $MmcArchive -WhatIf:$WhatIfPreference
}
