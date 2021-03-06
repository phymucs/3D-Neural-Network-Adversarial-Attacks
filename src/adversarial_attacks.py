import tensorflow as tf
from math import pi

def rotate_op(rot):
    point = tf.constant([1, 0, 0])
    point = point[:, tf.newaxis]

    pitch = rot[0][0]
    yaw = rot[0][1]

    pitch_mat = tf.stack([
        1, 0, 0,
        0, tf.cos(pitch), -tf.sin(pitch),
        0, tf.sin(pitch), tf.cos(pitch)
    ])
    pitch_mat = tf.reshape(pitch_mat, [3, 3])
    yaw_mat = tf.stack([
        tf.cos(yaw), -tf.sin(yaw), 0,
        tf.sin(yaw), tf.cos(yaw), 0,
        0, 0, 1
    ])
    yaw_mat = tf.reshape(yaw_mat, [3, 3])

    return tf.reshape(tf.matmul(yaw_mat, tf.matmul(pitch_mat, point)), [1, -1])

def view_op(x_pl, model_loss_fn, t_pl = None, one_hot = True, iter = 10, eps = 1):
    targeted = t_pl is not None
    alpha = eps / float(iter)

    # use the prediction class to prevent label leaking
    if not targeted:
        logits, _ = model_loss_fn(x_pl, None)
        t_pl = tf.argmax(logits, axis = 1)
        if one_hot:
            t_pl = tf.one_hot(t_pl, tf.shape(logits)[1])
        t_pl = tf.stop_gradient(t_pl)
    
    rot = tf.random_uniform([1, 2], maxval = pi * 2)
    for _ in range(iter):
        point = rotate_op(rot)
        dists = tf.linalg.norm(x_pl - point[:, tf.newaxis], axis = 2)
        min_dist = tf.reduce_min(dists, axis = 1)
        avg_dist = tf.reduce_mean(dists, axis = 1)
        dists = (dists - avg_dist[:, tf.newaxis]) / (avg_dist[:, tf.newaxis] - min_dist[:, tf.newaxis])
        dists = 1.0 - tf.sigmoid(dists * 6.0)
        x_adv = dists[:, :, tf.newaxis] * x_pl

        _, loss = model_loss_fn(x_adv, t_pl)
        grad = tf.gradients(loss, rot)[0]
        if targeted:
            rot = rot - alpha * grad
        else:
            rot = rot + alpha * grad
        rot = tf.stop_gradient(rot)
    
    remove = dists < 0.4
    idx = tf.argmin(tf.to_float(remove), axis = 1)
    one_hot = tf.one_hot(idx, tf.shape(x_adv)[1])
    replace = tf.reduce_sum(x_adv * one_hot[:, :, tf.newaxis], axis = 1, keep_dims = True)
    x_adv = tf.where(remove[:, :, tf.newaxis] & tf.fill(tf.shape(x_adv), True), replace + tf.zeros_like(x_adv), x_adv)
    x_adv = tf.stop_gradient(x_adv)

    return x_adv

def sort_op(x_pl, model_loss_fn, t_pl = None, faces = None, one_hot = True, iter = 10):
    targeted = t_pl is not None

    # use the prediction class to prevent label leaking
    if not targeted:
        logits, _ = model_loss_fn(x_pl, None)
        t_pl = tf.argmax(logits, axis = 1)
        if one_hot:
            t_pl = tf.one_hot(t_pl, tf.shape(logits)[1])
        t_pl = tf.stop_gradient(t_pl)

    if faces is not None:
        normal = tf.cross(faces[:, :, 1] - faces[:, :, 0], faces[:, :, 2] - faces[:, :, 1])
        normal = normal / tf.linalg.norm(normal, axis = 2, keep_dims = True)

    x_adv = x_pl
    var = tf.Variable(x_adv, trainable = False, collections = ["not_in_checkpoint"])
    with tf.control_dependencies([tf.variables_initializer([var])]):
        for _ in range(iter):
            _, loss = model_loss_fn(x_adv, t_pl)
            grad = tf.reduce_mean(tf.gradients(loss, x_adv)[0], axis = 2)
            #grad_sorted = tf.contrib.framework.argsort(grad, axis = 1)
            
            #idx = tf.tile(tf.range(tf.shape(x_adv)[0])[:, tf.newaxis], multiples = [1, iter])
            #lower = grad_sorted[:, :iter]
            #higher = grad_sorted[:, -iter:][:, ::-1]
            idx = tf.range(tf.shape(x_adv)[0])[:, tf.newaxis]
            lower = tf.to_int32(tf.argmin(grad, axis = 1)[:, tf.newaxis])
            higher = tf.to_int32(tf.argmax(grad, axis = 1)[:, tf.newaxis])
            lower = tf.reshape(tf.stack([idx, lower], axis = 2), shape = [-1, 2])
            higher = tf.reshape(tf.stack([idx, higher], axis = 2), shape = [-1, 2])

            if targeted:
                # replace higher with lower
                x_adv = tf.scatter_nd_update(var, higher, tf.gather_nd(x_adv, lower) + 1e-3)
            else:
                # replace lower with higher
                x_adv = tf.scatter_nd_update(var, lower, tf.gather_nd(x_adv, higher) + 1e-3)
            
            x_adv = tf.assign(var, x_adv)
            x_adv = tf.stop_gradient(x_adv)
    
    return x_adv

def iter_grad_op(x_pl, model_loss_fn, t_pl = None, faces = None, one_hot = True, iter = 10, eps = 0.01, restrict = False, ord = "inf", clip_min = None, clip_max = None, clip_norm = None, min_norm = 0.0):
    targeted = t_pl is not None
    alpha = eps / float(iter)
    if clip_norm is not None:
        dists = x_pl[:, tf.newaxis] - x_pl[:, :, tf.newaxis]
        dists = tf.linalg.norm(dists, axis = 3)

        diag = tf.eye(tf.shape(x_pl)[1], batch_shape = [tf.shape(x_pl)[0]])
        dists = tf.where(diag > 0.0, tf.fill(tf.shape(dists), float("inf")), dists)
        
        dists = tf.reduce_min(dists, axis = 2)
        avg, var = tf.nn.moments(dists, axes = [1], keep_dims = True)
        std = clip_norm * tf.sqrt(var)
        clip_norm = avg + std # set clip_norm to be the actual clip value
        
        clip_norm = clip_norm / float(iter)
    min_norm = min_norm / float(iter)

    # use the prediction class to prevent label leaking
    if not targeted:
        logits, _ = model_loss_fn(x_pl, None)
        t_pl = tf.argmax(logits, axis = 1)
        if one_hot:
            t_pl = tf.one_hot(t_pl, tf.shape(logits)[1])
        t_pl = tf.stop_gradient(t_pl)

    if faces is not None:
        normal = tf.cross(faces[:, :, 1] - faces[:, :, 0], faces[:, :, 2] - faces[:, :, 1])
        normal = normal / tf.linalg.norm(normal, axis = 2, keep_dims = True)

    if ord == "inf":
        ord_fn = tf.sign
    elif ord == "1":
        ord_fn = lambda x: x / tf.reduce_sum(tf.abs(x), axis = list(range(1, x.shape.ndims)), keep_dims = True)
    elif ord == "2":
        ord_fn = lambda x: x / tf.sqrt(tf.reduce_sum(x ** 2, axis = list(range(1, x.shape.ndims)), keep_dims = True))
    elif ord == "2.5":
        def ord_fn(x):
            norm = tf.linalg.norm(x, axis = -1, keep_dims = True)
            return tf.where(tf.equal(norm, 0.0) & tf.fill(tf.shape(x), True), tf.zeros_like(x), x / norm)
    else:
        raise ValueError("Only L-inf, L1, L2, and normalized L2 norms are supported!")

    x_adv = x_pl
    for _ in range(iter):
        _, loss = model_loss_fn(x_adv, t_pl)

        x_original = x_adv

        perturb = alpha * ord_fn(tf.gradients(loss, x_adv)[0])
        perturb_norm = tf.linalg.norm(perturb, axis = -1, keep_dims = True)
        if clip_norm is not None:
            clip = perturb_norm > clip_norm[..., tf.newaxis]
            perturb = tf.where(clip & tf.fill(tf.shape(perturb), True), perturb * clip_norm[..., tf.newaxis] / perturb_norm, perturb)
        perturb = perturb * tf.to_float(perturb_norm >= min_norm)

        if targeted:
            x_adv = x_adv - perturb
        else:
            x_adv = x_adv + perturb

        if faces is not None:
            # constrain perturbations for each point to its corresponding plane
            x_adv = x_adv - normal * tf.reduce_sum(normal * (x_adv - faces[:, :, 0]), axis = 2, keep_dims = True)
            # clip perturbations that goes outside each triangle
            if restrict:
                x_adv = triangle_border_intersections_op(x_original, x_adv, faces)

        if clip_min is not None and clip_max is not None:
            x_adv = tf.clip_by_value(x_adv, clip_min, clip_max)
        
        x_adv = tf.stop_gradient(x_adv)
    
    return x_adv

def momentum_grad_op(x_pl, model_loss_fn, t_pl = None, faces = None, one_hot = True, iter = 10, eps = 0.01, momentum = 1.0, restrict = False, ord = "inf", clip_min = None, clip_max = None, clip_norm = None, min_norm = 0.0):
    targeted = t_pl is not None
    alpha = eps / float(iter)
    if clip_norm is not None:
        dists = x_pl[:, tf.newaxis] - x_pl[:, :, tf.newaxis]
        dists = tf.linalg.norm(dists, axis = 3)

        diag = tf.eye(tf.shape(x_pl)[1], batch_shape = [tf.shape(x_pl)[0]])
        dists = tf.where(diag > 0.0, tf.fill(tf.shape(dists), float("inf")), dists)
        
        dists = tf.reduce_min(dists, axis = 2)
        avg, var = tf.nn.moments(dists, axes = [1], keep_dims = True)
        std = clip_norm * tf.sqrt(var)
        clip_norm = avg + std # set clip_norm to be the actual clip value

        clip_norm = clip_norm / float(iter)
    min_norm = min_norm / float(iter)

    # use the prediction class to prevent label leaking
    if not targeted:
        logits, _ = model_loss_fn(x_pl, None)
        t_pl = tf.argmax(logits, axis = 1)
        if one_hot:
            t_pl = tf.one_hot(t_pl, tf.shape(logits)[1])
        t_pl = tf.stop_gradient(t_pl)

    if faces is not None:
        normal = tf.cross(faces[:, :, 1] - faces[:, :, 0], faces[:, :, 2] - faces[:, :, 1])
        normal = normal / tf.linalg.norm(normal, axis = 2, keep_dims = True)

    if ord == "inf":
        ord_fn = tf.sign
    elif ord == "1":
        ord_fn = lambda x: x / tf.reduce_sum(tf.abs(x), axis = list(range(1, x.shape.ndims)), keep_dims = True)
    elif ord == "2":
        ord_fn = lambda x: x / tf.sqrt(tf.reduce_sum(x ** 2, axis = list(range(1, x.shape.ndims)), keep_dims = True))
    elif ord == "2.5":
        def ord_fn(x):
            norm = tf.linalg.norm(x, axis = -1, keep_dims = True)
            return tf.where(tf.equal(norm, 0.0) & tf.fill(tf.shape(x), True), tf.zeros_like(x), x / norm)
    else:
        raise ValueError("Only L-inf, L1, L2, and normalized L2 norms are supported!")

    x_adv = x_pl
    prev_grad = tf.zeros_like(x_pl)
    for _ in range(iter):
        _, loss = model_loss_fn(x_adv, t_pl)

        grad = tf.gradients(loss, x_adv)[0]
        grad = grad / tf.reduce_mean(tf.abs(grad), axis = list(range(1, x_pl.shape.ndims)), keep_dims = True)
        grad = momentum * prev_grad + grad
        prev_grad = grad

        x_original = x_adv

        perturb = alpha * ord_fn(grad)
        perturb_norm = tf.linalg.norm(perturb, axis = -1, keep_dims = True)
        if clip_norm is not None:
            clip = perturb_norm > clip_norm[..., tf.newaxis]
            perturb = tf.where(clip & tf.fill(tf.shape(perturb), True), perturb * clip_norm[..., tf.newaxis] / perturb_norm, perturb)
        perturb = perturb * tf.to_float(perturb_norm >= min_norm)

        if targeted:
            x_adv = x_adv - perturb
        else:
            x_adv = x_adv + perturb

        if faces is not None:
            # constrain perturbations for each point to its corresponding plane
            x_adv = x_adv - normal * tf.reduce_sum(normal * (x_adv - faces[:, :, 0]), axis = 2, keep_dims = True)
            # clip perturbations that goes outside each triangle
            if restrict:
                x_adv = triangle_border_intersections_op(x_original, x_adv, faces)

        if clip_min is not None and clip_max is not None:
            x_adv = tf.clip_by_value(x_adv, clip_min, clip_max)
        
        x_adv = tf.stop_gradient(x_adv)
    
    return x_adv

def jacobian_saliency_map_points_op(x_pl, model_loss_fn, t_pl = None, faces = None, one_hot = True, iter = 10, eps = 0.01, restrict = False, clip_min = None, clip_max = None):
    targeted = t_pl is not None
    
    # use the prediction class to prevent label leaking
    if not targeted:
        logits, _ = model_loss_fn(x_pl, None)
        t_pl = tf.argmax(logits, axis = 1)
        t_pl = tf.stop_gradient(t_pl)
    
    if targeted and one_hot:
        t_pl = tf.argmax(t_pl, axis = 1)
        t_pl = tf.stop_gradient(t_pl)

    if faces is not None:
        normal = tf.cross(faces[:, :, 1] - faces[:, :, 0], faces[:, :, 2] - faces[:, :, 1])
        normal = normal / tf.linalg.norm(normal, axis = 2, keep_dims = True)

    x_adv = x_pl
    unused = tf.fill(tf.shape(x_adv)[:2], True)
    for _ in range(iter):
        logits, _ = model_loss_fn(x_adv, None)

        total_grad = tf.gradients(logits, x_adv)[0]
        target_grad = tf.gradients(tf.reduce_sum(tf.stop_gradient(tf.one_hot(t_pl, tf.shape(logits)[1])) * logits, axis = 1), x_adv)[0]
        other_grad = total_grad - target_grad

        saliency = tf.abs(target_grad) * tf.abs(other_grad)
        increase = (target_grad >= 0.0) & (other_grad <= 0.0) & unused[:, :, tf.newaxis]
        decrease = (target_grad <= 0.0) & (other_grad >= 0.0) & unused[:, :, tf.newaxis]
        saliency = saliency * tf.to_float(increase | decrease)
        saliency = tf.reduce_sum(saliency, axis = 2)

        idx = tf.argmax(saliency, axis = 1)
        one_hot = tf.one_hot(idx, tf.shape(saliency)[1], on_value = True, off_value = False)
        increase = increase & one_hot[:, :, tf.newaxis]
        decrease = decrease & one_hot[:, :, tf.newaxis]
        unused = unused & ~one_hot

        x_original = x_adv

        perturb = tf.to_float(increase) * tf.fill(tf.shape(x_adv), -eps) + tf.to_float(decrease) * tf.fill(tf.shape(x_adv), eps)

        if targeted:
            x_adv = x_adv - perturb
        else:
            x_adv = x_adv + perturb

        if faces is not None:
            # constrain perturbations for each point to its corresponding plane
            x_adv = x_adv - normal * tf.reduce_sum(normal * (x_adv - faces[:, :, 0]), axis = 2, keep_dims = True)
            # clip perturbations that goes outside each triangle
            if restrict:
                x_adv = triangle_border_intersections_op(x_original, x_adv, faces)

        if clip_min is not None and clip_max is not None:
            x_adv = tf.clip_by_value(x_adv, clip_min, clip_max)

        x_adv = tf.stop_gradient(x_adv)
    
    return x_adv

def jacobian_saliency_map_pair_op(x_pl, model_loss_fn, t_pl = None, faces = None, one_hot = True, iter = 10, eps = 0.01, restrict = False, clip_min = None, clip_max = None):
    targeted = t_pl is not None
    
    # use the prediction class to prevent label leaking
    if not targeted:
        logits, _ = model_loss_fn(x_pl, None)
        t_pl = tf.argmax(logits, axis = 1)
        t_pl = tf.stop_gradient(t_pl)
    
    if targeted and one_hot:
        t_pl = tf.argmax(t_pl, axis = 1)
        t_pl = tf.stop_gradient(t_pl)

    if faces is not None:
        normal = tf.cross(faces[:, :, 1] - faces[:, :, 0], faces[:, :, 2] - faces[:, :, 1])
        normal = normal / tf.linalg.norm(normal, axis = 2, keep_dims = True)

    x_adv = x_pl
    size = tf.reduce_prod(tf.shape(x_adv)[1:])
    unused = tf.fill([tf.shape(x_adv)[0], size], True)
    for _ in range(iter):
        logits, _ = model_loss_fn(x_adv, None)

        total_grad = tf.gradients(logits, x_adv)[0]
        target_grad = tf.gradients(tf.reduce_sum(tf.stop_gradient(tf.one_hot(t_pl, tf.shape(logits)[1])) * logits, axis = 1), x_adv)[0]
        other_grad = total_grad - target_grad

        saliency = tf.abs(target_grad) * tf.abs(other_grad)
        saliency = tf.reshape(saliency, [-1, size])
        saliency = saliency[:, tf.newaxis, :] + saliency[:, :, tf.newaxis]
        target_grad = tf.reshape(target_grad, [-1, size])
        other_grad = tf.reshape(other_grad, [-1, size])
        target_grad = target_grad[:, tf.newaxis, :] + target_grad[:, :, tf.newaxis]
        other_grad = other_grad[:, tf.newaxis, :] + other_grad[:, :, tf.newaxis]

        if targeted:
            # target should increase, others should decrease
            cond = unused[:, tf.newaxis, :] & unused[:, :, tf.newaxis] & (target_grad >= 0.0) & (other_grad <= 0.0)
        else:
            # others should increase, target should decrease
            cond = unused[:, tf.newaxis, :] & unused[:, :, tf.newaxis] & (target_grad <= 0.0) & (other_grad >= 0.0)

        diag_zeros = tf.ones([size, size])
        diag_zeros = tf.linalg.set_diag(diag_zeros, tf.zeros(size))

        idx_both = tf.argmax(tf.reshape(tf.to_float(cond) * diag_zeros[tf.newaxis, :, :] * saliency, [-1, size * size]), axis = 1)
        i = tf.to_int64(idx_both / tf.to_int64(size))
        j = tf.to_int64(idx_both % tf.to_int64(size))
        perturb = tf.one_hot(i, size, on_value = eps, off_value = 0.0) + tf.one_hot(j, size, on_value = eps, off_value = 0.0)
        perturb = tf.reshape(perturb, tf.shape(x_adv))
        unused = unused & tf.one_hot(i, size, on_value = False, off_value = True) & tf.one_hot(j, size, on_value = False, off_value = True)

        x_original = x_adv
        x_adv = x_adv + perturb

        if faces is not None:
            # constrain perturbations for each point to its corresponding plane
            x_adv = x_adv - normal * tf.reduce_sum(normal * (x_adv - faces[:, :, 0]), axis = 2, keep_dims = True)
            # clip perturbations that goes outside each triangle
            if restrict:
                x_adv = triangle_border_intersections_op(x_original, x_adv, faces)

        if clip_min is not None and clip_max is not None:
            x_adv = tf.clip_by_value(x_adv, clip_min, clip_max)

        x_adv = tf.stop_gradient(x_adv)
    
    return x_adv

inf = float("inf")
float_epsilon = 1e-4 # to handle floating point inaccuracies

def triangle_border_intersections_op(p1, p2, triangles):
    p = p1
    d = p2 - p1

    triangle_normals = tf.cross(triangles[:, :, 1] - triangles[:, :, 0], triangles[:, :, 2] - triangles[:, :, 1])

    side_normals = tf.stack([
        tf.cross(triangles[:, :, 1] - triangles[:, :, 0], triangles[:, :, 1] - triangles[:, :, 0] + triangle_normals),
        tf.cross(triangles[:, :, 2] - triangles[:, :, 1], triangles[:, :, 2] - triangles[:, :, 1] + triangle_normals),
        tf.cross(triangles[:, :, 0] - triangles[:, :, 2], triangles[:, :, 0] - triangles[:, :, 2] + triangle_normals)
    ], axis = 2)

    # intersection between line and triangle sides as planes
    dot = tf.reduce_sum(side_normals * d[:, :, tf.newaxis, :], axis = 3)
    zero_mask = tf.equal(dot, 0.0)
    dot = tf.where(zero_mask, tf.ones_like(dot), dot) # prevent division by zero
    a = p[:, :, tf.newaxis, :] - triangles
    b = -tf.reduce_sum(side_normals * a, axis = 3) / dot
    dir = d[:, :, tf.newaxis, :] * b[:, :, :, tf.newaxis]
    # intersections not in the same direction as d have b < 0
    mask = tf.logical_or(zero_mask, b < -float_epsilon)[:, :, :, tf.newaxis] & tf.fill(tf.shape(dir), True)
    dir = tf.where(mask, tf.fill(tf.shape(dir), inf), dir)

    # only use closest intersection
    dists = tf.linalg.norm(dir, axis = 3)
    min_idx = tf.argmin(dists, axis = 2)
    closest_mask = tf.one_hot(min_idx, 3, on_value = True, off_value = False)[:, :, :, tf.newaxis] & tf.fill(tf.shape(dir), True)
    dir = tf.where(closest_mask, dir, tf.zeros_like(dir))
    dir = tf.reduce_sum(dir, axis = 2)
    # either use the intersection point or d
    dists = tf.linalg.norm(dir, axis = 2)
    norm_d = tf.linalg.norm(d, axis = 2)
    closest_mask = (norm_d < dists)[:, :, tf.newaxis] & tf.fill(tf.shape(dir), True)
    dir = tf.where(closest_mask, d, dir)

    return p + dir