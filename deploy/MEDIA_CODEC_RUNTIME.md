# Versioned Source-Media Codecs

Candidate tooling, not production activation. Uploaded media needs patched
codecs as well as namespace/resource isolation. An ordinary Ubuntu package can
lack ESM fixes even after refreshing its signed index. Do not interpret a
successful sandbox self-test or a high upstream version number as a complete
current vulnerability clearance.

## Build Authority

`media-codecs.lock.json` pins official FFmpeg9.0.2 source bytes and the release
signing fingerprint independently published on
[FFmpeg's download page](https://ffmpeg.org/download.html). The detached
signature must verify against that exact authority; HTTPS or a same-download
checksum alone is insufficient. An upstream update requires explicit lock,
signature, security-notice, compatibility and native qualification review.

`media_codec_runtime.py build` runs only from clean committed source on an
unprivileged Linuxx86_64 runner. It produces fully static FFmpeg/ffprobe,
disables network support and hardware devices, and enables only the required
output encoders/muxers. The sandbox remains mandatory. The build uses signed
Ubuntu22.04 distribution inputs and downloads the exact installed x264,
glibc and zlib source packages from its signed source index. Those source
archives, the official FFmpeg source/signature/key, build script/configuration,
GCC runtime copyright, dependency versions and a CycloneDX inventory accompany
the binaries. Static-link and version checks must pass. This inventory covers
the stated source/static libraries, not every build-host OS package or GPU.

The Media codec runtime workflow builds candidate artifacts on pull requests.
Only protected-main pushes attest the exact archive, manifest, inventory and
checksums. No workflow installs tools on a server or opens a source path.
The same artifact must still pass Gallery's actual preparation tests and both
services' real user/restriction checks before source activation. Local mocked
archive tests or a different compiled binary do not establish that evidence.

## Verify And Stage

1. Require the reviewed exact commit's ordinary release gates and codec job
   green. Verify GitHub attestation repository, main ref, hosted-runner signer
   workflow, source commit and exact archive digest before using its contents.
2. Use the verifier from that independently reviewed commit. It requires the
   expected40-character source SHA, exact source lock, bounded expansion,
   committed regular files, hashes/modes and no links/devices/extra paths.
   Extraction only creates a fresh private stage and never selects a runtime.
3. Recheck current upstream/vendor security notices and the actual static
   dependency versions. Preserve a compatible rollback and enough disk; never
   overwrite `/usr/bin` or upgrade system libraries as part of this rollout.
4. Stage root-owned under a distinct versioned codec directory. For each
   candidate service, set its private trusted PATH to that exact `bin` first;
   verify both resolved paths and manifest hashes before startup. A missing
   selected binary or fallback to a system codec fails the deployment check.
   Do not publish internal paths or copy application credentials into a bundle.
5. The Linux launcher bind-mounts only the resolved executable read-only at
   `/codec/tool`; it does not expose the executable's parent directory. Host
   network/app files/environment remain absent, with zero effective
   capabilities and unchanged CPU/address-space/output/descriptor limits.
6. Qualify the real selected binaries under actual service restrictions with
   source admission closed. Restore/rollback checks and bounded native funded
   tests still gate any opening. Rollback selects the previous qualified codec
   runtime or closes source admission; it never falls back to an unpatched
   codec to preserve source availability.

Building, inventory, provenance and installation are separate from creative
quality, private-input cleanup, soundtrack alignment and once-only billing.

## Rebuild

The bundle contains upstream source and exact distribution source packages,
including their packaging patches. `manifest.json` records the compiler,
dependency versions and full configure arguments. Rebuild the distribution
libraries from those sources, then run the recorded FFmpeg configure and
`make -j2 ffmpeg ffprobe`; no private Grid repository, key or server credential
is needed to rebuild the codec programs. Keep the corresponding source and
license material available with redistributed binaries.
