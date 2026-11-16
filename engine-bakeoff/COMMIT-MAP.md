# Commit map

Before round 1 was published, the Foundation rewrote this repository's history once. The rewrite removed the names of unrelated software from the first pass's host records and from two commit messages, removed local file paths from the pass logs, and dropped the internal runbooks for two benchmark machines, a PC and the AWS host. Dropping the second left one commit empty, so the rewrite left that commit out, and the table below has no row for it. The rewrite also reworded one code comment that named the AWS host's runbook. Apart from those changes it changed no result, code, or method text, and it changed no author, date, or commit subject.

Every commit ID changed, so the IDs that this repository cites in its own text no longer match its history. A result's `repository_commit` in `environment.json`, the commit lines in the pass logs, and a few commit messages and READMEs cite the original IDs. This table maps each original commit to its published counterpart, oldest first. A `repository_commit` is the 40-character form of an ID, and it begins with the 12 characters in the first column. The commits after the last row of this table are new in the published repository.

*Each row maps one original commit to its published counterpart, with its date and subject.*

| Original | Published | Date | Subject |
| --- | --- | --- | --- |
| `ee852fed81c9` | [`ee852fed81c9`](https://github.com/OpenAR-Collective/spikes/commit/ee852fed81c97241e3a94473975c0162a2350e71) | 2026-10-08 | Set up the spikes repository and the engine bake-off |
| `aca0cc3072b0` | [`aca0cc3072b0`](https://github.com/OpenAR-Collective/spikes/commit/aca0cc3072b0cd4cf1e261b0478bc484c710b6e5) | 2026-10-08 | Import the engine slice of Andy Troiano's HiveAR prototype |
| `9ef70e6b3cac` | [`9ef70e6b3cac`](https://github.com/OpenAR-Collective/spikes/commit/9ef70e6b3cacbef76391f75f4088423685011d10) | 2026-10-08 | Trim the Python side to the engine and record what was kept |
| `48dd8c96ed04` | [`48dd8c96ed04`](https://github.com/OpenAR-Collective/spikes/commit/48dd8c96ed040b51266c55e79a43d855097e79b9) | 2026-10-08 | Close the method gaps the Python import surfaced |
| `80d7cdb82d79` | [`80d7cdb82d79`](https://github.com/OpenAR-Collective/spikes/commit/80d7cdb82d79eaaa6b903c1772021bb06516cf61) | 2026-10-08 | Fold Rob's decisions into the bake-off method |
| `8f74efafa5a9` | [`8f74efafa5a9`](https://github.com/OpenAR-Collective/spikes/commit/8f74efafa5a9125632b94657380ac3d97c93c735) | 2026-10-08 | Build the C# and Marten side of the engine bake-off |
| `f3f8f71074c8` | [`f3f8f71074c8`](https://github.com/OpenAR-Collective/spikes/commit/f3f8f71074c879ec5899e008c3c28e98809c4982) | 2026-10-08 | Pin the Python side's event hash to the shared vectors |
| `3c342ee15f00` | [`3c342ee15f00`](https://github.com/OpenAR-Collective/spikes/commit/3c342ee15f00da71dd9527f73143f9ebb95a111f) | 2026-10-08 | Specify the workloads both sides implement |
| `3c8bd8b909f3` | [`3c8bd8b909f3`](https://github.com/OpenAR-Collective/spikes/commit/3c8bd8b909f3847e7a1677aa69082cd66ae6ed46) | 2026-10-08 | Settle the workload details both sides must match |
| `c570b9628bee` | [`c570b9628bee`](https://github.com/OpenAR-Collective/spikes/commit/c570b9628bee953f8161c1cddfc14e3809022b0c) | 2026-10-08 | Reword a sentence to the house style |
| `68c04c6b96f1` | [`68c04c6b96f1`](https://github.com/OpenAR-Collective/spikes/commit/68c04c6b96f1f5b39af77a886a0311ab93dc5dcc) | 2026-10-08 | Build the Python side of the bake-off workloads |
| `0bd09d7ebc70` | [`0bd09d7ebc70`](https://github.com/OpenAR-Collective/spikes/commit/0bd09d7ebc702332b1f34edd982b7ad3808afe9d) | 2026-10-08 | Implement the workloads on the C# side |
| `c31d6e49c70b` | [`c31d6e49c70b`](https://github.com/OpenAR-Collective/spikes/commit/c31d6e49c70b9b8ad0a3d0d5468a1e679cb29498) | 2026-10-08 | Add the progress view and say that K sets both |
| `baaf59ee4cf6` | [`baaf59ee4cf6`](https://github.com/OpenAR-Collective/spikes/commit/baaf59ee4cf67395d04052f17b2b773a12056e42) | 2026-10-08 | Add a Compose override for browsing the C# database |
| `2da55970965b` | [`2da55970965b`](https://github.com/OpenAR-Collective/spikes/commit/2da55970965bf0080f1b7cea93c45328e4dd5185) | 2026-10-08 | Add a direct append variant to the C# side |
| `5702f5661991` | [`5702f5661991`](https://github.com/OpenAR-Collective/spikes/commit/5702f5661991064cc243afb6023e9bcd3f4e5029) | 2026-10-08 | Let --rule-workers set the Python side's pollers too |
| `a434da62aa27` | [`a434da62aa27`](https://github.com/OpenAR-Collective/spikes/commit/a434da62aa27f36270ea6f71b761cbfbff63105a) | 2026-10-08 | Add the harness that measures every side from outside |
| `66e919bc297f` | [`66e919bc297f`](https://github.com/OpenAR-Collective/spikes/commit/66e919bc297fa23f3ad1c9a12d7721e004440adb) | 2026-10-08 | Set the first measured pass and fix verify-chain's output |
| `c7d08ffe0794` | [`c7d08ffe0794`](https://github.com/OpenAR-Collective/spikes/commit/c7d08ffe0794db805b100a05ac1aeec4480d6f8c) | 2026-10-08 | Let long C# operations outlast Npgsql's 30-second default |
| `3e435f8341d9` | [`5165e8892ccf`](https://github.com/OpenAR-Collective/spikes/commit/5165e8892ccfea7ef06f33946c74fc1e31bcccaa) | 2026-10-09 | Record the first side-by-side pass |
| `e009d23255ca` | [`ac3ead6113b9`](https://github.com/OpenAR-Collective/spikes/commit/ac3ead6113b94c50e6d36a3d18930df29f7f0478) | 2026-10-09 | Correct the first pass summary and add the database ceiling |
| `bde5ba87bf5b` | [`204c7709cde1`](https://github.com/OpenAR-Collective/spikes/commit/204c7709cde119f3aab5a2ea4b1e397256808747) | 2026-10-09 | Say that the first pass's real-time CPU figures read a third high |
| `3d56ec539217` | [`7de95c0b9c35`](https://github.com/OpenAR-Collective/spikes/commit/7de95c0b9c35b0af24fef44c5147066e68e4c151) | 2026-10-09 | Correct how catch-up and real-time CPU are measured |
| `9a82c357a6c4` | [`ac94874e83f5`](https://github.com/OpenAR-Collective/spikes/commit/ac94874e83f59fc94a7034e954463fe448879b8c) | 2026-10-09 | Measure catch-up to the end head and CPU per command over sent_total |
| `31aa1a049ccc` | [`03c407364d5b`](https://github.com/OpenAR-Collective/spikes/commit/03c407364d5b423aa496e46fb8956210265ce2bb) | 2026-10-09 | Divide statements and commits per command the same way as CPU |
| `2e0452e2c15f` | [`eb55c926354d`](https://github.com/OpenAR-Collective/spikes/commit/eb55c926354d614026c876d02542828241235ed4) | 2026-10-09 | Add bulk workloads to the method |
| `ddd240b2c57a` | [`e5b700e8de57`](https://github.com/OpenAR-Collective/spikes/commit/e5b700e8de571dad92addec7937b6b949f41c4d5) | 2026-10-09 | Build the bulk workloads on the C# side |
| `7088e4d1c31b` | [`14bec54322a7`](https://github.com/OpenAR-Collective/spikes/commit/14bec54322a7a3f97d39f9075579cc0c1552b37c) | 2026-10-09 | Point the C# README at the first pass's results |
| `2cbf191ed716` | [`82668d32d8c5`](https://github.com/OpenAR-Collective/spikes/commit/82668d32d8c53a58b86d87ab3e740928d9dae440) | 2026-10-09 | Copy the C# side as the start of a Marten-free side |
| `547185000e7d` | [`03c91715f202`](https://github.com/OpenAR-Collective/spikes/commit/03c91715f2028d55bb36f1d8c7598d51eff52ba2) | 2026-10-09 | Build the bulk workloads on the Python side and in the harness |
| `20d7db8ba923` | [`522e6e8d04a2`](https://github.com/OpenAR-Collective/spikes/commit/522e6e8d04a29548453fa58c31a8be46145cd66c) | 2026-10-09 | Mask the partition hash instead of taking its absolute value |
| `90e6d3475b5f` | [`0fddd5555e15`](https://github.com/OpenAR-Collective/spikes/commit/0fddd5555e151e952ca5d204f1382335bc564401) | 2026-10-09 | Replace Marten with Wax's own event store on the csharp-wax side |
| `5cfadc0cabce` | [`83d8912e5054`](https://github.com/OpenAR-Collective/spikes/commit/83d8912e505407884d140e3bc6d5ef083a2f4680) | 2026-10-09 | Add the hashing-off diagnostic and fix the progress view |
| `5d133b6e3195` | [`54efe374708c`](https://github.com/OpenAR-Collective/spikes/commit/54efe374708c2fd0c1561235ca70bb4486c0ebfa) | 2026-10-09 | Add the engine-paused, hashing-off, and top-statements diagnostics |
| `fa797d735de6` | [`aa0014cd3ecb`](https://github.com/OpenAR-Collective/spikes/commit/aa0014cd3ecb5dabcf603624712a50574a97abcf) | 2026-10-09 | Record the host machine in every run's environment |
| `da81d2224f4d` | [`06f8c0d31833`](https://github.com/OpenAR-Collective/spikes/commit/06f8c0d3183372ec0c8c51531418c6eb948173d8) | 2026-10-09 | Say in the harness README what environment.json records about the host |
| `9eb6fcf5ae89` | [`bf5a7328c207`](https://github.com/OpenAR-Collective/spikes/commit/bf5a7328c2071d56ed7367f4eabbf02bba07f513) | 2026-10-09 | Plan the benchmark passes on the PC and write its runbook |
| `79fdd84ec9c4` | [`2d232fec7079`](https://github.com/OpenAR-Collective/spikes/commit/2d232fec7079076b98225683dcc6d09d37121a90) | 2026-10-09 | Describe the benchmark PC and how to set it up |
| `a70bd4fb176f` | [`454a215a22d3`](https://github.com/OpenAR-Collective/spikes/commit/454a215a22d36fc6a772669eaacef5aa8ce6bd43) | 2026-10-09 | Record the host's Python and Compose versions in environment.json |
| `604223b71af7` | [`dba3b8af144a`](https://github.com/OpenAR-Collective/spikes/commit/dba3b8af144aa95b2a3c9d7b5af95e92f5a32e40) | 2026-10-09 | Fix tonight's second pass on the laptop and its tuned settings |
| `44b2648b76c7` | [`6a0d01ca7377`](https://github.com/OpenAR-Collective/spikes/commit/6a0d01ca7377d9f30eb9eea8dd25a01427464f4d) | 2026-10-10 | Record the second pass |
| `d9f6baaf09d5` | [`d95c72d0dfc4`](https://github.com/OpenAR-Collective/spikes/commit/d95c72d0dfc4d79385a5142aad2324442c018579) | 2026-10-10 | Record CPU throttling and a host speed probe for every run |
| `059303af1190` | [`a4d71e5019c7`](https://github.com/OpenAR-Collective/spikes/commit/a4d71e5019c746840e09c731557d4322915dd762) | 2026-10-10 | Write the second pass into the README's results |
| `70aff5996112` | [`aac87caa5b52`](https://github.com/OpenAR-Collective/spikes/commit/aac87caa5b525eecd897c55217181c0f9e0be83b) | 2026-10-10 | Plan the third pass |
| `d9a8e88d6d79` | [`b65ffb45fab8`](https://github.com/OpenAR-Collective/spikes/commit/b65ffb45fab8bc4db4b30e099a2cb6996e0d22a2) | 2026-10-10 | Record the third pass, cut to group 1 |
| `4d279553efca` | [`c7d34413156c`](https://github.com/OpenAR-Collective/spikes/commit/c7d34413156c7aad17f6ffa504a7f55f0de89009) | 2026-10-10 | Write the third pass's group 1 into the README's results |
| `3d3c3534738a` | [`6c50f3145bad`](https://github.com/OpenAR-Collective/spikes/commit/6c50f3145badb76bc18e554fd15c6c723fa6e1e1) | 2026-10-10 | Let the pass runner stop a run on Linux |
| `7cdfdff37ff1` | [`a1ae9a6770bc`](https://github.com/OpenAR-Collective/spikes/commit/a1ae9a6770bc888e7c1b9912f34443031098ac0e) | 2026-10-10 | Wait for the engine to settle, and record steal time, write activity, and a Linux host |
| `32301babf2d1` | [`a540b6e6d43f`](https://github.com/OpenAR-Collective/spikes/commit/a540b6e6d43f227c827d0c72b736b00a1cd71c49) | 2026-10-10 | Note the engine start-up flaw in the README's limits |
| `daad54697dbb` | [`47c828298bdb`](https://github.com/OpenAR-Collective/spikes/commit/47c828298bdb6b0887eb522190eced0396ac14ed) | 2026-10-10 | Record Rob's approval of the engine-readiness wait |
| `8c1212adff80` | [`f3e76498e555`](https://github.com/OpenAR-Collective/spikes/commit/f3e76498e5554a89aa829789a61321056b42f649) | 2026-10-10 | Keep Python at eight rule workers after the 500k re-test |
| `d3c85cb39ca2` | [`e9c0c69d05ab`](https://github.com/OpenAR-Collective/spikes/commit/e9c0c69d05ab19f5110b79b5e58a5a5142235dd1) | 2026-10-10 | Add the scaled caps profile for the AWS host |
| `da3cbdb5a32e` | [`1f07381aa5f9`](https://github.com/OpenAR-Collective/spikes/commit/1f07381aa5f9d72e814f7de62207ab486da1be2c) | 2026-10-10 | Plan the AWS campaign's four passes |
| `74512b509942` | [`745854f73250`](https://github.com/OpenAR-Collective/spikes/commit/745854f73250432bd166c3627bcbd86d6853069a) | 2026-10-10 | Add a caps flag that selects the standard or scaled profile |
| `c095b406f426` | [`1d0bcf560111`](https://github.com/OpenAR-Collective/spikes/commit/1d0bcf560111494a51e474541ada89adfae71053) | 2026-10-10 | Add a pgBouncer pool size flag and account for pgBouncer's CPU |
| `0c9146e7af2a` | [`10757ff1f59e`](https://github.com/OpenAR-Collective/spikes/commit/10757ff1f59e0d624f3c91be4d977dcd14a0a3c3) | 2026-10-10 | Check the host and clean up before each pass |

## The second rewrite

The commits of the EF Core mini-pass and of round 2's laptop pass were rewritten once more before they were published. As in the first rewrite, the names of unrelated software in the host records became `unrelated-container`, and local file paths left the pass logs. Personal references became neutral wording in the two plans, the script that writes plans, the script that judges the EF Core mini-pass, this folder's README, and four commit messages. Two commit messages also stated more than the recorded results support, and they were corrected to what the results show: the placement pair and the noise floor of the EF Core mini-pass, and, for round 2, its background, the shape of the two sides' writes, and the payment file's rules. Apart from those changes the rewrite changed no result, code, or method text, and it changed no author, date, or commit subject.

The two commits that needed no change and the two python-direct commits, which were already public on the branch `round2/python-direct`, kept their IDs. The EF Core mini-pass's results cite the original ID of its plan commit, and round 2's cite the original ID of theirs. This table maps each original commit to its published counterpart, oldest first.

*Each row maps one original commit of the second rewrite to its published counterpart, with its date and subject.*

| Original | Published | Date | Subject |
| --- | --- | --- | --- |
| `41c97ae60240` | [`41c97ae60240`](https://github.com/OpenAR-Collective/spikes/commit/41c97ae60240860d23f2efee0e73b00086147f9f) | 2026-10-11 | Add an EF Core state mode to csharp-wax, run as csharp-wax-ef |
| `054af7d677de` | [`054af7d677de`](https://github.com/OpenAR-Collective/spikes/commit/054af7d677debe8038a54cc0f0354f2121737795) | 2026-10-11 | Tune the EF Core mode before the recorded mini-pass |
| `fb62dd045ba4` | [`9451307b2338`](https://github.com/OpenAR-Collective/spikes/commit/9451307b233877f6779dc999ca77bb6b63c6bb61) | 2026-10-11 | Plan the EF Core mini-pass |
| `753e9f3e4e26` | [`98596ab63d52`](https://github.com/OpenAR-Collective/spikes/commit/98596ab63d52d6b18d063362da74731af2941efe) | 2026-10-11 | Record the EF Core mini-pass |
| `e4dc5033bb58` | [`1f5310b2a34f`](https://github.com/OpenAR-Collective/spikes/commit/1f5310b2a34fdb79b9692998c128ab7e2287fc14) | 2026-10-11 | Add the script that judges the EF Core mini-pass |
| `d7956dbff61e` | [`ad5d13d7eea1`](https://github.com/OpenAR-Collective/spikes/commit/ad5d13d7eea1fc52b9a6882dfa214856e5644bca) | 2026-10-11 | Mend the make_plan docstring after the efcore example |
| `8bc5639c2be0` | [`8bc5639c2be0`](https://github.com/OpenAR-Collective/spikes/commit/8bc5639c2be06a253f9933cb855b10f42d279e66) | 2026-10-11 | Add the python-direct side: the Python command path in plain SQL |
| `08dd8c5dadb9` | [`08dd8c5dadb9`](https://github.com/OpenAR-Collective/spikes/commit/08dd8c5dadb9708e419080d631d57a100b54d0ac) | 2026-10-11 | Move python-direct's rule runners and register pollers off Django |
| `d31d7f1b1c07` | [`2cbf3858b01e`](https://github.com/OpenAR-Collective/spikes/commit/2cbf3858b01e1bae4fbb55125660fd90bd2a00cb) | 2026-11-12 | Merge round2/python-direct into round2/laptop-pass for round 2's laptop pass |
| `e192ec74f7e0` | [`bda684b8e45f`](https://github.com/OpenAR-Collective/spikes/commit/bda684b8e45f19ea70dfe951d74feb0d3b7180bd) | 2026-11-12 | Plan round 2's pass on the laptop |
| `ecdae8a65f32` | [`f0aecd8ea4d0`](https://github.com/OpenAR-Collective/spikes/commit/f0aecd8ea4d0e4dd54e83771af8d8c63f9efc8fe) | 2026-11-13 | Record round 2's pass on the laptop |
| `4c8e5ea363bb` | [`80fc1937a181`](https://github.com/OpenAR-Collective/spikes/commit/80fc1937a18113ea4650bd0edf3478e51ca78ca8) | 2026-11-13 | Add the script that tabulates round 2's laptop pass |

## The third rewrite

The commits of round 2's code-shape pass were made on top of the laptop pass's original history, from before the second rewrite, so they were replayed onto the published history before they were published. The commit that adds the batched mode was replayed onto the published `main` with its content unchanged, and the merge that brings it in was made again with the same two-parent shape and the same message. The merge's tree is therefore the measured tree with the second rewrite's changes and the write-ups published with it applied, and its code is the measured code. As in the earlier rewrites, the names of unrelated software in the host records became `unrelated-container`, and local file paths left the pass log. A personal reference became neutral wording in the plan, the script that writes plans, and the plan's commit message. The results commit's message stated two figures more strongly than the recorded results support, and they were corrected to what the results show: how closely the noise pair agreed, and the placement rate with the engine paused. Where the replay met a conflict in the bake-off README, it kept the published text and added the pass's entry. Apart from those changes the replay changed no result, code, or method text. It kept every author, author date, and commit subject, and the commit dates are those of the replay.

The pass's results cite the original ID of its plan commit, and its plan and test record cite the original ID of the merge, which they call the measured tree. This table maps each original commit to its published counterpart, oldest first.

*Each row maps one original commit of the third rewrite to its published counterpart, with its author date and subject.*

| Original | Published | Date | Subject |
| --- | --- | --- | --- |
| `a9bd48e8c6d4` | [`042b6a993b75`](https://github.com/OpenAR-Collective/spikes/commit/042b6a993b7564f9ddd95b1d2c228dfa3245efc9) | 2026-11-13 | Add a batched state mode to csharp-wax, run as csharp-wax-batched |
| `c8fa969195d4` | [`6c1bc6442c81`](https://github.com/OpenAR-Collective/spikes/commit/6c1bc6442c81f0546c68dec256d17e164381b1b7) | 2026-11-13 | Merge round2/csharp-batched into round2/shape-pass for the code-shape pass |
| `4a318a737eb3` | [`7a1a2ae73f9f`](https://github.com/OpenAR-Collective/spikes/commit/7a1a2ae73f9fbc0616c486f45f647e06ecbae4d9) | 2026-11-13 | Plan round 2's code-shape pass |
| `2f0820732b32` | [`7b9474dc92dd`](https://github.com/OpenAR-Collective/spikes/commit/7b9474dc92ddc2b7073fabcad2ab45b474884b19) | 2026-11-13 | Record round 2's code-shape pass |
| `a8f0bba2a23c` | [`2cc0d239e9f7`](https://github.com/OpenAR-Collective/spikes/commit/2cc0d239e9f754ddaa569a7e2f489d40cc82e64f) | 2026-11-14 | Add the script that tabulates round 2's code-shape pass |

## The fourth rewrite

The commits of round 2's fix pass were rewritten once before they were published. Two commit messages credited reviews by name, and that wording became neutral, so the first of those commits and every commit after it changed ID. As in the earlier rewrites, the names of unrelated software in the host records became `unrelated-container`, and local file paths left the pass log. Apart from those changes the rewrite changed no result, code, or method text. It kept every author, author date, and commit subject, and the commit dates are those of the rewrite. The two commits before the first of them, which add the fix and make the driver settings csharp-wax's own, kept their IDs.

The pass's results and its log cite the original ID of its plan commit, and its plan and test record cite the original ID of the commit they call the measured tree. This table maps each original commit to its published counterpart, oldest first.

*Each row maps one original commit of the fourth rewrite to its published counterpart, with its author date and subject.*

| Original | Published | Date | Subject |
| --- | --- | --- | --- |
| `fd98f9394d0f` | [`550ad8648564`](https://github.com/OpenAR-Collective/spikes/commit/550ad8648564c4d98a29aac4a54c059f6b0e133d) | 2026-11-15 | Apply the reviews of the reader fix |
| `17a9837349ab` | [`49c46c6aee46`](https://github.com/OpenAR-Collective/spikes/commit/49c46c6aee46447c5216ecc208992560381430ab) | 2026-11-15 | Describe csharp-wax's driver settings in the sides table |
| `d34b97f48d79` | [`0530b296e754`](https://github.com/OpenAR-Collective/spikes/commit/0530b296e754ebf84bad473fe0d98b507047ede5) | 2026-11-15 | Plan the pass that tests the reader fix |
| `a29d435c0350` | [`cea683378bb0`](https://github.com/OpenAR-Collective/spikes/commit/cea683378bb0b45ff90fefb885fe7909958320e2) | 2026-11-16 | Record the pass that tests the reader fix |
| `59e95f3034bd` | [`93dec172ae0d`](https://github.com/OpenAR-Collective/spikes/commit/93dec172ae0d248b0ff07e4b0ef060ca02b155d2) | 2026-11-16 | Add the script that tabulates the pass that tests the reader fix |
| `735ef0b34a19` | [`8f8be1742c2e`](https://github.com/OpenAR-Collective/spikes/commit/8f8be1742c2e7a40923f5024d9a5b4a1df344d2b) | 2026-11-16 | Tell the rate regime apart from the readers' cadence in the fix tables |
