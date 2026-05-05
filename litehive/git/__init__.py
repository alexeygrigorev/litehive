"""
Git side-effecting subsystem.

Single owner module ``litehive.git.ops`` for every ``subprocess.run(["git", ...])``
in the project (see code-style R: side-effecting subsystems). Other packages
import the typed wrappers there instead of shelling out themselves; that keeps
sandboxing, error translation, and ``cwd`` discipline in one place.
"""
