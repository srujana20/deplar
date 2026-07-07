import express from 'express'
import { getUser, createUser } from './handlers'

const router = express.Router()

router.get('/v1/users/:id', async (req, res) => {
    res.json(await getUser(req.params.id))
})

router.post('/users', async (req, res) => {
    res.json(await createUser(req.body))
})

export default router
