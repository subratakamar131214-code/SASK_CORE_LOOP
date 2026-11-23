# Runbook: secrets

## Storage rules

- GitHub Actions Secrets only. Repository scope for pipeline keys; environment
  scope (`swarm-approval`) for the replication token where possible.
- Local development uses exported environment variables. Nothing reads `.env`.
- Never commit, print, log, or paste a credential. Never place one in tests,
  fixtures, docs, issues, PRs, artifacts, or generated products.

## Add a secret

GitHub UI: Settings → Secrets and variables → Actions → New repository secret.

- `GROQ_API_KEY` — free-tier Groq key
- `GEMINI_API_KEY` — free-tier Google AI Studio key
- `FINE_GRAINED_GH_TOKEN` — optional; only for approved child repository creation

CLI option that avoids shell history and process arguments:

```bash
read -rs GROQ_API_KEY
printf '%s' "$GROQ_API_KEY" | gh secret set GROQ_API_KEY
unset GROQ_API_KEY
```

Repeat with the correct name. For an environment-scoped secret, add
`--env swarm-approval`.

## Rotate

1. Create the replacement credential at the provider with the minimum scope.
2. Update the Actions Secret (UI or the command above).
3. Dispatch `revenue-cycle` (or `swarm-replicate`) and confirm success without
   printing the value.
4. Revoke the old credential.
5. If the old value could have been logged, purge workflow logs/artifacts that
   contain it. Treat public exposure as permanent.

## Leak response

1. **Revoke immediately** at the provider. Rotation after revocation, not before.
2. Replace the Actions Secret.
3. Search for the value with GitHub secret scanning and locally:
   ```bash
   python -m src.compliance.secretscan --tree .
   git log -S '<a short unique fragment>' --all
   ```
   Never paste the full credential into a command that will be logged.
4. If it reached git history, coordinate a history rewrite with maintainers and
   force-push only after everyone has preserved work. Revocation is mandatory
   regardless of cleanup.
5. Record what was exposed, where, when, and which rotations were performed in a
   private incident note. Do not publish the secret or its replacement.

## Verify the scanner

The scanner is fail-closed; a finding blocks the state commit. A clean scan is not
proof of absence of sensitive free text, so keep raw feedback out of git and
keep prompts sanitized.

## Fine-grained token scope

`FINE_GRAINED_GH_TOKEN` is used only by the manual replication workflow. Grant
only what repository creation requires, expire it, and store it at the narrowest
scope available. If repository creation is not used, do not create the token.
