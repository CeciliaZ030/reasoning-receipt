# Verify, prepare, simulate, approve, verify the outcome
1. Take the user's receipt JSON exactly as given and call rr_prepare_settlement. The API
   checks every commitment in the receipt and simulates the transfer from the connected
   wallet. If either fails, nothing is prepared; tell the user why and stop.
2. Present the preview: receipt hash, chain, token, recipient, amount in base units, and
   the paying wallet. Say that preparing is unsigned and moves nothing.
3. Follow the stage_tx route byte for byte. The host simulates the staged transaction
   right away. If that simulation fails, stop: nothing may be submitted.
4. Ask for explicit approval of these exact details. Only then call commit_txs for that
   one staged transaction, and let the user approve it in their wallet. Never use
   unattended signing. If the user rejects the wallet prompt, nothing was paid; stop.
5. With the wallet's transaction_hash, call rr_verify_settlement. Only status settled is
   success, and it returns the new outcome receipt that links to the original.
   pending or pending_finality: wait and verify the same hash again. reverted: no funds
   moved and no outcome exists; the user may start again from step 1.
If the broadcast result is unknown, recover the hash from the wallet and verify it. Never
submit a second transaction to find out.
