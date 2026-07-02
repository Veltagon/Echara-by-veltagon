# ECHARA v2 operating guide

## Mission
Build a simple, reliable orchestration agent that delivers real working software. Keep the implementation practical, modular, and easy to verify.

## Core rules
- Finish the user-requested work fully before stopping.
- Prefer real implementation over placeholders or stubs whenever possible.
- Keep code simple, clean, and maintainable.
- Do not dump everything into one file; split responsibilities into clear modules.
- Avoid unnecessary complexity and extra features.
- And structure them properly.
- make sure that not all the files are liying scarttered, only 4-5  code files can be lieing in the root dir, other      things should be inside folder(subfolders if required)
## Verification standard
Before claiming success, verify all of the following:
- Does the code actually run?
- Does it produce the expected files and state transitions?
- Does it avoid fake or incomplete behavior that only looks correct?

## Working style
- Use small, focused steps.
- Keep the project structure organized.
- Prefer readable code over clever code.
- Only run meaningful tests or builds that provide real signal.
