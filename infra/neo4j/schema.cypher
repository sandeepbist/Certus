CREATE CONSTRAINT certus_tenant_id_unique IF NOT EXISTS
FOR (tenant:Tenant) REQUIRE tenant.id IS UNIQUE;

CREATE CONSTRAINT certus_user_id_unique IF NOT EXISTS
FOR (user:User) REQUIRE user.id IS UNIQUE;

CREATE CONSTRAINT certus_document_id_unique IF NOT EXISTS
FOR (document:Document) REQUIRE document.id IS UNIQUE;

CREATE CONSTRAINT certus_task_id_unique IF NOT EXISTS
FOR (task:Task) REQUIRE task.id IS UNIQUE;

CREATE CONSTRAINT certus_entity_tenant_name_unique IF NOT EXISTS
FOR (entity:Entity) REQUIRE (entity.tenant_id, entity.normalized_name) IS UNIQUE;

CREATE INDEX certus_entity_type IF NOT EXISTS
FOR (entity:Entity) ON (entity.tenant_id, entity.type);

// Co-mention paths are derived through Document nodes. Remove legacy global
// edges, which could not be reversed safely when a document was replaced.
MATCH ()-[relationship:RELATED_TO {type: 'co_occurrence'}]->()
DELETE relationship;
