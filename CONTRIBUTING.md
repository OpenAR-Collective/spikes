# Contributing

Thank you for helping with an experiment. Read the README of the experiment you are working on first: it states the question, the method, and the rules for that experiment, and a change that alters the method needs agreement before it lands.

## Developer Certificate of Origin

Contributions are accepted under the [Developer Certificate of Origin, Version 1.1](https://developercertificate.org/), per Article IV of the Foundation's Open Source Policy. By signing off on a commit, you attest that the contribution is your own work, or that you have the right to submit it under the applicable license.

Every commit must carry a `Signed-off-by` trailer with your real name and email address:

```
Signed-off-by: Jane Developer <jane@example.com>
```

Git adds this for you when you commit with the `-s` flag:

```
git commit -s -m "Describe your change"
```

The Foundation does not require a Contributor License Agreement; you retain copyright in your contributions.

## Licensing of contributions

All contributions are accepted under the Apache License, Version 2.0 (see [LICENSE](LICENSE)). If you bring in code from another source, it must carry a license compatible with Apache 2.0, and you must credit it in [NOTICE](NOTICE) and in the README of the folder that holds it. A file you change after bringing it in gets a header comment saying where it came from and that it was modified.

## Practical notes

- Text files use LF line endings, which [.gitattributes](.gitattributes) enforces. Shell scripts with CRLF endings fail inside Linux containers.
- Foundation materials use US English.
- Results are recorded as the experiment's README describes, with the raw output kept unedited. A result that is inconvenient is still a result.
