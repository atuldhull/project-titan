// TITAN-SEC-072 proof fixture: rendering untrusted HTML via
// dangerouslySetInnerHTML MUST fail `no-restricted-syntax`. Never imported by
// app code — it exists only so `make lint-proofs` proves the ban fires.
export function DangerousHtml({ html }: { html: string }) {
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}
