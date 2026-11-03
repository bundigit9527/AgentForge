from agentforge_core.models import RunConflict, RunNotFound
from agentforge_core.skills import SkillHub
from agentforge_storage.skills import SkillRepository
from agentforge_storage.user_skills import UserSkillRepository

from agentforge_skills.builtin import BUILTIN_SKILLS
from agentforge_skills.markdown import resolve_document


class SkillService:
    def __init__(self, repository: SkillRepository, hub: SkillHub):
        self.repository = repository
        self.hub = hub
        self.builtin = {skill.reference: skill for skill in BUILTIN_SKILLS}
        self.user = UserSkillRepository(
            repository.engine, repository.clock, auth_mode=repository.authorization.mode
        )
        self.reserved_names = {skill.manifest.name.casefold() for skill in BUILTIN_SKILLS}

    def list(self, scope, offset, limit):
        return [entry[0] for entry in self.catalog(scope)[offset : offset + limit]]

    def catalog(self, scope, *, connection=None):
        if connection is None:
            with self.repository.engine.connect() as conn:
                return self.catalog(scope, connection=conn)
        installed = self.repository.catalog(scope, connection=connection)
        personal = self.user.catalog(scope, connection=connection)
        return [(skill, 0.0, 0.0) for skill in self.builtin.values()] + installed + personal

    def resolve(self, scope, references):
        resolved = []
        for reference in references:
            if reference.startswith("builtin:"):
                skill = self.builtin.get(reference)
                if skill is None:
                    raise RunNotFound()
            else:
                try:
                    skill = self.repository.get(scope, reference)
                except RunNotFound:
                    skill = self.user.resolve(scope, reference)
            resolved.append(skill)
        return tuple(resolved)

    def create_user(self, scope, fields):
        return self.user.create(scope, resolve_document(fields), self.reserved_names)

    def update_user(self, scope, skill_id, fields, expected_version=None):
        return self.user.update(
            scope,
            skill_id,
            lambda prior: resolve_document(fields, prior),
            self.reserved_names,
            expected_version=expected_version,
        )

    def install(self, scope, request):
        if request.source_id != self.hub.source_id:
            raise RunConflict("Skill Hub source has changed; reload the catalog")
        manifest = self.hub.manifest(request.skill_id, request.version)
        if manifest.digest() != request.sha256:
            raise RunConflict("Skill manifest has changed; reload the catalog")
        return self.repository.install(scope, self.hub.source_id, manifest, request.sha256)
