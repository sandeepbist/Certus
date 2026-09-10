import dotenv from 'dotenv';
import { fileURLToPath } from 'node:url';

// ESM dependencies run before the importing module body. Keeping dotenv in a
// first side-effect import ensures route-level URL constants see local values.
dotenv.config({ path: fileURLToPath(new URL('../../../../.env', import.meta.url)) });
