/**
 * Browser side of the WebAuthn ceremonies (issue #745, phase 2c).
 *
 * The server speaks base64url (that is what `options_to_json` emits and what the verification
 * helpers expect back); `navigator.credentials` speaks ArrayBuffer. This file is the ONE place
 * that translation happens — doing it inline at each call site is how one field ends up
 * base64-standard and the ceremony fails with an error nobody can read.
 */

export type PublicKeyOptions = Record<string, unknown>

export function isPasskeySupported(): boolean {
  return typeof window !== 'undefined' && !!window.PublicKeyCredential
}

function b64urlToBuffer(value: string): ArrayBuffer {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/')
  const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4))
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

function bufferToB64url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i])
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** Decode the base64url fields the server sends into the buffers the browser API demands. */
function decodeOptions(options: PublicKeyOptions): PublicKeyCredentialCreationOptions {
  const decoded = { ...options } as Record<string, unknown>
  decoded.challenge = b64urlToBuffer(options.challenge as string)
  const user = options.user as { id: string } | undefined
  if (user) decoded.user = { ...user, id: b64urlToBuffer(user.id) }
  for (const key of ['excludeCredentials', 'allowCredentials']) {
    const list = options[key] as Array<{ id: string }> | undefined
    if (list) decoded[key] = list.map((c) => ({ ...c, id: b64urlToBuffer(c.id) }))
  }
  return decoded as unknown as PublicKeyCredentialCreationOptions
}

function encodeCredential(credential: PublicKeyCredential): Record<string, unknown> {
  const response = credential.response as AuthenticatorAttestationResponse &
    AuthenticatorAssertionResponse
  const encoded: Record<string, unknown> = {
    id: credential.id,
    rawId: bufferToB64url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults(),
    authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
    response: {
      clientDataJSON: bufferToB64url(response.clientDataJSON),
    } as Record<string, unknown>,
  }
  const inner = encoded.response as Record<string, unknown>
  if (response.attestationObject) inner.attestationObject = bufferToB64url(response.attestationObject)
  if (response.authenticatorData) inner.authenticatorData = bufferToB64url(response.authenticatorData)
  if (response.signature) inner.signature = bufferToB64url(response.signature)
  if (response.userHandle) inner.userHandle = bufferToB64url(response.userHandle)
  return encoded
}

/** Run the registration ceremony. Returns null when the user dismisses the prompt — a cancelled
 * ceremony is a choice, not an error to surface as a failure. */
export async function createPasskey(options: PublicKeyOptions): Promise<Record<string, unknown> | null> {
  const credential = (await navigator.credentials.create({
    publicKey: decodeOptions(options),
  })) as PublicKeyCredential | null
  return credential ? encodeCredential(credential) : null
}

/** Run the authentication ceremony. Same contract as above. */
export async function getPasskeyAssertion(
  options: PublicKeyOptions,
): Promise<Record<string, unknown> | null> {
  const credential = (await navigator.credentials.get({
    publicKey: decodeOptions(options) as unknown as PublicKeyCredentialRequestOptions,
  })) as PublicKeyCredential | null
  return credential ? encodeCredential(credential) : null
}
