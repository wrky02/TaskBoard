param([Parameter(Mandatory=$true)][string[]]$Paths)
$ErrorActionPreference='Stop'
foreach ($path in $Paths) {
    $signature=Get-AuthenticodeSignature $path
    if ($signature.Status -ne 'Valid') { throw "Signature is not trusted: $path ($($signature.Status))" }
    if (-not $signature.TimeStamperCertificate) { throw "RFC3161 timestamp is missing: $path" }
    Write-Output "Verified: $path | Signer: $($signature.SignerCertificate.Subject) | Thumbprint: $($signature.SignerCertificate.Thumbprint)"
}
