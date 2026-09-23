# Security profile

Exact sources and conformed raw remain `restricted-unclassified`. Git-crypt filters cover the closed legal archive layout; examples are wholly synthetic and explicitly exempt. Effective coverage is checked using `git check-attr`, including discovered paths, rather than text matching. Git-crypt protects committed objects only when configured and does not protect an unlocked worktree, filenames, size/timing metadata, caches, backups, or logs.

Before commit/release, operators must run the staged-ciphertext gate after git-crypt is configured. It examines every staged protected Git blob and requires the git-crypt ciphertext header regardless of media type; it never prints content. CI cannot decrypt and therefore cannot prove encryption. Key management, locked-worktree inspection, no-network execution of generated adapters, temporary storage protection, and incident response are explicit operational controls.
