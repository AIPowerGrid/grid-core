<!--
SPDX-FileCopyrightText: 2026 AI Power Grid

SPDX-License-Identifier: AGPL-3.0-or-later
-->

<div class="logo-section">
<img src="{logo_url}" alt="AI Power Grid Logo" class="logo">
<h2>API</h2>
<p>A decentralized AI inference network providing high-performance access to cutting-edge AI models through a robust, scalable API infrastructure.</p>
</div>

## Network Status

<div class="grid">

<div class="stat">
<div class="stat-value">{image_workers}</div>
<div class="stat-label">Image Workers</div>
</div>

<div class="stat">
<div class="stat-value">{video_workers}</div>
<div class="stat-label">Video Workers</div>
</div>

<div class="stat">
<div class="stat-value">{text_workers}</div>
<div class="stat-label">Text Workers</div>
</div>

<div class="stat">
<div class="stat-value">{total_workers}</div>
<div class="stat-label">Total Workers</div>
</div>

<div class="stat">
<div class="stat-value">{total_threads}</div>
<div class="stat-label">Total Threads</div>
</div>

<div class="stat">
<div class="stat-value">{interrogation_workers}</div>
<div class="stat-label">Interrogation Workers</div>
</div>

</div>

### Lifetime Performance

<div class="grid">

<div class="stat">
<div class="stat-value">{total_image_fulfillments}{total_image_fulfillments_char}</div>
<div class="stat-label">Image Requests</div>
</div>

<div class="stat">
<div class="stat-value">{total_image_things} {total_total_image_things_name}</div>
<div class="stat-label">Megapixels</div>
</div>

<div class="stat">
<div class="stat-value">{avg_performance} {avg_thing_name}/sec</div>
<div class="stat-label">Avg Image Speed</div>
</div>

<div class="stat">
<div class="stat-value">{total_text_fulfillments}{total_text_fulfillments_char}</div>
<div class="stat-label">Text Requests</div>
</div>

<div class="stat">
<div class="stat-value">{total_text_things} {total_text_things_name}</div>
<div class="stat-label">Tokens</div>
</div>

<div class="stat">
<div class="stat-value">{avg_text_performance} {avg_text_thing_name}/sec</div>
<div class="stat-label">Avg Text Speed</div>
</div>

</div>

### Current Queue Status

<div class="grid-2">

<div class="stat">
<div class="stat-value">{total_image_queue}</div>
<div class="stat-label">Current Image Requests</div>
</div>

<div class="stat">
<div class="stat-value">{queued_image_things} {queued_image_things_name}</div>
<div class="stat-label">Queued Megapixels</div>
</div>

<div class="stat">
<div class="stat-value">{total_text_queue}</div>
<div class="stat-label">Current Text Requests</div>
</div>

<div class="stat">
<div class="stat-value">{queued_text_things} {queued_text_things_name}</div>
<div class="stat-label">Queued Tokens</div>
</div>

</div>

### Available Models

<div class="card">

<details>
<summary><strong>Image Models ({image_models_count})</strong></summary>

<div class="model-list">

{image_models_list}

</div>

</details>

</div>

<div class="card">

<details>
<summary><strong>Video Models ({video_models_count})</strong></summary>

<div class="model-list">

{video_models_list}

</div>

</details>

</div>

<div class="card">

<details>
<summary><strong>Text Models ({text_models_count})</strong></summary>

<div class="model-list">

{text_models_list}

</div>

</details>

</div>

### Top Performing Models

<div class="card">

<details>
<summary><strong>Most Requested Models</strong></summary>

<div class="model-list">

{top_models_list}

</div>

</details>

</div>

## Get Started

<div class="cta-section">
<a href="/register" class="cta-button">Create Account & Get API Key</a>
<p class="cta-subtext">Free to use. No credit card required.</p>
</div>

## API Documentation & Resources

<div class="grid">

<div class="card">
<h3>API Dashboard</h3>
<p>Interactive API reference with Scalar UI</p>
<a href="https://dashboard.aipowergrid.io/api-doc">dashboard.aipowergrid.io/api-doc</a>
</div>

<div class="card">
<h3>Swagger / OpenAPI</h3>
<p>Raw OpenAPI specification for integrations</p>
<a href="https://api.aipowergrid.io/api/swagger.json">api.aipowergrid.io/api/swagger.json</a>
</div>

<div class="card">
<h3>Legacy API Docs</h3>
<p>Classic Swagger UI documentation</p>
<a href="https://api.aipowergrid.io/api/docs">api.aipowergrid.io/api/docs</a>
</div>

<div class="card">
<h3>Source Code</h3>
<p>Open source repositories and examples</p>
<a href="https://github.com/aipowergrid">github.com/aipowergrid</a>
</div>

<div class="card">
<h3>Community</h3>
<p>Developer support and discussions</p>
<a href="https://discord.gg/aipowergrid">Discord Server</a>
</div>

<div class="card">
<h3>Official SDK</h3>
<p>Python client library for AI Power Grid</p>
<a href="https://github.com/AIPowerGrid/grid-sdk">Grid SDK on GitHub</a>
</div>

</div>

