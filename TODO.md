# Roost TODOs

Lines starting with "- " render on the Fleet board (item -- detail). Delete when done.

- Register the Mac mini key with dokku on the Pi -- On the Pi: echo '<mini ~/.ssh/id_ed25519.pub>' | sudo dokku ssh-keys:add mini. Until then the mini's hourly status refresh fails at the push step (self-heals once added).
- Optional: gh auth login on the Mac mini -- without it the clauffice "CI — recent runs" section only refreshes on MacBook pushes.
