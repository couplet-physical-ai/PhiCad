// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) 2026 Exowatt, Inc. / KiCad Developers.
//
// PhiCad owns no model runtime and no credential. The standalone Agent View launches the
// signed-in user's phi process; this explicit extension adds only application attribution to
// the request that phi already makes. Identity and spend ownership remain on the per-user key.

export const CLIENT_TAG = "client=phicad";
const HEADER = "x-litellm-tags";

/**
 * Add PhiCad's LiteLLM spend tag without discarding tags another phi extension supplied.
 *
 * Header names are case-insensitive but the extension hook exposes a plain object. Collapse any
 * spelling to one lowercase field so fetch cannot serialize two competing headers. Setting an
 * old spelling to null is phi's documented way to remove it.
 */
export function addPhiCadClientTag(headers) {
  const matching = Object.keys(headers).filter((key) => key.toLowerCase() === HEADER);
  const tags = matching
    .flatMap((key) => String(headers[key] ?? "").split(","))
    .map((tag) => tag.trim())
    .filter(Boolean);

  if (!tags.includes(CLIENT_TAG)) tags.push(CLIENT_TAG);
  for (const key of matching) {
    if (key !== HEADER) headers[key] = null;
  }
  headers[HEADER] = [...new Set(tags)].join(",");
}

export default function registerPhiCadAttribution(phi) {
  phi.on("before_provider_headers", (event) => {
    addPhiCadClientTag(event.headers);
  });
}
