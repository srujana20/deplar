import axios from 'axios'
import { UserSchema } from '../shared/schema'

const USER_SERVICE_URL = process.env.USER_SERVICE_URL

export async function getUser(id: string) {
    const r = await axios.get(`${USER_SERVICE_URL}/users/${id}`)
    return r.data
}

export async function updateUser(id: string, data: unknown) {
    const r = await axios.post('https://user-service.internal/users', data)
    return r.data
}
