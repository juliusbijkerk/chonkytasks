# Recording the demo clip

Glance at this while you record. Target: **12 to 15 minutes**, Dunelm first,
then IKEA for the same product.

```bash
./pipeline/record.sh shopping
```

Press `q` to stop. You get `out/shopping.mp4` and `out/shopping.wav`.

## Before you press enter

- [ ] Grant Screen Recording and Microphone to your terminal.
      **macOS makes you restart the terminal after granting screen recording.**
      Do this first, not when you are ready to go.
- [ ] Close mail, Slack, Discord, 1Password, anything with a notification.
- [ ] New browser window, one tab. Not your normal window with 12 tabs.
- [ ] Log out of Dunelm if you are logged in. We do not want an account in frame.
- [ ] Mic actually picking you up. Say something, check the level.

## What to do on camera

Buy a mattress protector, properly, the way you actually would. Then stop at
the basket. **Do not complete a purchase.**

Do the loop more than twice. The ranker only counts a pattern it sees at least
twice, so one pass of each site gives you nothing.

1. Search Dunelm for it
2. Open a product, look at price, size, material, reviews
3. Go back, open another, compare
4. Note the one you like somewhere (a notes file, a spreadsheet, anything)
5. Repeat for three or four products
6. Same again on IKEA
7. Add your pick to the basket, stop there

## What to say out loud

The narration is half the product. Vision sees the click; your voice is the
only source of **why**. Say the rule, not the action.

Good:
> "Under fifty reviews I ignore the rating entirely, it is noise."
> "I am skipping this one, waterproof always means it sleeps hot."
> "Anything over thirty quid for a protector is not worth it to me."
> "I check the depth before anything else, our mattress is deep."

Useless:
> "Now I am clicking on the search box."
> "Scrolling down."

If you go quiet for a minute, that is fine. If you narrate every click, the
transcript becomes noise and the ranker gets worse, not better.

## Say your constraints once, near the start

One clean sentence early gives the model the frame for everything after:

> "I want a cotton mattress protector for a deep double, under about twenty five
> pounds, from somewhere that delivers this week."

## Afterwards

```bash
./pipeline/transcribe.py out/shopping.wav      # -> out/narration.txt
./pipeline/sample_frames.py out/shopping.mp4 --every 3
./pipeline/observe.py
./pipeline/rank.py
```

Read `out/narration.txt` before you run `observe.py`. If the transcript is
rubbish, the rest of the pipeline cannot save it, and you want to know in the
thirty seconds it takes to look rather than after three vision passes.

## If something sensitive got in frame

Delete the mp4 and record again. It is twelve minutes. Do not try to edit around
it, and do not send it to the API "just to see".
