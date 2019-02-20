import imgaug
import torch
from torch.utils.data import Dataset
import numpy as np

from mrcnn.utils import utils
from mrcnn.models.components import anchors
from tools.config import Config
from tools.time_profiling import profilable


# Augmentors that are safe to apply to masks
# Some, such as Affine, have settings that make them unsafe, so always
# test your augmentation on masks
MASK_AUGMENTERS = ["Sequential", "SomeOf", "OneOf", "Sometimes",
                   "Fliplr", "Flipud", "CropAndPad",
                   "Affine", "PiecewiseAffine"]


def load_image_gt(dataset_handler, image_id, use_mini_mask=False,
                  augmentation=None):
    """Load and return ground truth data for an image (image, mask,
       bounding boxes).

    augment: If true, apply random image augmentation. Currently, only
        horizontal flipping is offered.
    use_mini_mask: If False, returns full-size masks that are the same height
        and width as the original image. These can be big, for example
        1024x1024x100 (for 100 instances). Mini masks are smaller, typically,
        224x224 and are generated by extracting the bounding box of the
        object and resizing it to MINI_MASK_SHAPE.

    Returns:
    image: [height, width, 3]
    shape: the original shape of the image before resizing and cropping.
    class_ids: [instance_count] Integer class IDs
    bbox: [instance_count, (y1, x1, y2, x2)]
    mask: [height, width, instance_count]. The height and width are those
        of the image unless use_mini_mask is True, in which case they are
        defined in MINI_MASK_SHAPE.
    """
    # Load image and mask
    image = dataset_handler.load_image(image_id)
    mask, class_ids = dataset_handler.load_mask(image_id)
    image, image_metas = utils.mold_image(image)
    mask = utils.resize_mask(mask, image_metas.scale,
                             image_metas.padding, image_metas.crop)

    # Augmentation
    # This requires the imgaug lib (https://github.com/aleju/imgaug)
    if augmentation:
        def hook(images, augmenter, parents, default):  # pylint: disable=W0613
            """Determines which augmenters to apply to masks."""
            return augmenter.__class__.__name__ in MASK_AUGMENTERS

        # Store shapes before augmentation to compare
        image_shape = image.shape
        mask_shape = mask.shape
        # Make augmenters deterministic to apply similarly to images and masks
        det = augmentation.to_deterministic()
        image = det.augment_image(image)
        # Change mask to np.uint8 because imgaug doesn't support np.bool
        mask = det.augment_image(mask.astype(np.uint8),
                                 hooks=imgaug.HooksImages(activator=hook))
        # Verify that shapes didn't change
        assert image.shape == image_shape, "Augmentation shouldn't change image size"
        assert mask.shape == mask_shape, "Augmentation shouldn't change mask size"
        # Change mask back to bool
        mask = mask.astype(np.bool)

    # Note that some boxes might be all zeros if the corresponding mask got
    # cropped out and here is to filter them out
    _idx = np.sum(mask, axis=(0, 1)) > 0
    mask = mask[:, :, _idx]
    class_ids = class_ids[_idx]

    # Bounding boxes. Note that some boxes might be all zeros
    # if the corresponding mask got cropped out.
    # bbox: [num_instances, (y1, x1, y2, x2)]
    bbox = utils.extract_bboxes(mask)

    # Active classes
    # Different dataset_handlers have different classes, so track the
    # classes supported in the dataset_handler of this image.
    active_class_ids = np.zeros([dataset_handler.num_classes], dtype=np.int32)
    source_class_ids = dataset_handler.source_class_ids[dataset_handler.image_info[image_id]["source"]]
    active_class_ids[source_class_ids] = 1

    # Resize masks to smaller size to reduce memory usage
    if use_mini_mask:
        mask = utils.minimize_masks(bbox, mask, Config.MINI_MASK.SHAPE)

    # Image meta data
    image_metas.active_class_ids = active_class_ids

    return image, image_metas, class_ids, bbox, mask


def build_rpn_targets(image_shape, anchors, gt_class_ids, gt_boxes):
    """Given the anchors and GT boxes, compute overlaps and identify positive
    anchors and deltas to refine them to match their corresponding GT boxes.

    anchors: [num_anchors, (y1, x1, y2, x2)]
    gt_class_ids: [num_gt_boxes] Integer class IDs.
    gt_boxes: [num_gt_boxes, (y1, x1, y2, x2)]

    Returns:
    rpn_match: [N] (int32) matches between anchors and GT boxes.
               1 = positive anchor, -1 = negative anchor, 0 = neutral
    rpn_bbox: [N, (dy, dx, log(dh), log(dw))] Anchor bbox deltas.
    """
    # RPN Match: 1 = positive anchor, -1 = negative anchor, 0 = neutral
    rpn_match = np.zeros([anchors.shape[0]], dtype=np.int32)
    # RPN bounding boxes: [max anchors per image, (dy, dx, log(dh), log(dw))]
    rpn_bbox = np.zeros((Config.RPN.ANCHOR.NB_PER_IMAGE, 4))

    # Handle COCO crowds
    # A crowd box in COCO is a bounding box around several instances. Exclude
    # them from training. A crowd box is given a negative class ID.
    crowd_ix = np.where(gt_class_ids < 0)[0]
    if crowd_ix.shape[0] > 0:
        # Filter out crowds from ground truth class IDs and boxes
        non_crowd_ix = np.where(gt_class_ids > 0)[0]
        crowd_boxes = gt_boxes[crowd_ix]
        gt_class_ids = gt_class_ids[non_crowd_ix]
        gt_boxes = gt_boxes[non_crowd_ix]
        # Compute overlaps with crowd boxes [anchors, crowds]
        crowd_overlaps = utils.compute_overlaps(anchors, crowd_boxes)
        crowd_iou_max = np.amax(crowd_overlaps, axis=1)
        no_crowd_bool = (crowd_iou_max < 0.001)
    else:
        # All anchors don't intersect a crowd
        no_crowd_bool = np.ones([anchors.shape[0]], dtype=bool)

    # Compute overlaps [num_anchors, num_gt_boxes]
    overlaps = utils.compute_overlaps(anchors, gt_boxes)

    # Match anchors to GT Boxes
    # If an anchor overlaps a GT box with IoU >= 0.7 then it's positive.
    # If an anchor overlaps a GT box with IoU < 0.3 then it's negative.
    # Neutral anchors are those that don't match the conditions above,
    # and they don't influence the loss function.
    # However, don't keep any GT box unmatched (rare, but happens). Instead,
    # match it to the closest anchor (even if its max IoU is < 0.3).
    #
    # 1. Set negative anchors first. They get overwritten below if a GT box is
    # matched to them. Skip boxes in crowd areas.
    anchor_iou_argmax = np.argmax(overlaps, axis=1)
    anchor_iou_max = overlaps[np.arange(overlaps.shape[0]), anchor_iou_argmax]
    rpn_match[(anchor_iou_max < 0.3) & (no_crowd_bool)] = -1
    # 2. Set an anchor for each GT box (regardless of IoU value).
    # TODO: If multiple anchors have the same IoU match all of them
    gt_iou_argmax = np.argmax(overlaps, axis=0)
    rpn_match[gt_iou_argmax] = 1
    # 3. Set anchors with high overlap as positive.
    rpn_match[anchor_iou_max >= 0.7] = 1

    # Subsample to balance positive and negative anchors
    # Don't let positives be more than half the anchors
    ids = np.where(rpn_match == 1)[0]
    extra = len(ids) - (Config.RPN.ANCHOR.NB_PER_IMAGE // 2)
    if extra > 0:
        # Reset the extra ones to neutral
        ids = np.random.choice(ids, extra, replace=False)
        rpn_match[ids] = 0
    # Same for negative proposals
    ids = np.where(rpn_match == -1)[0]
    extra = len(ids) - (Config.RPN.ANCHOR.NB_PER_IMAGE -
                        np.sum(rpn_match == 1))
    if extra > 0:
        # Rest the extra ones to neutral
        ids = np.random.choice(ids, extra, replace=False)
        rpn_match[ids] = 0

    # For positive anchors, compute shift and scale needed to transform them
    # to match the corresponding GT boxes.
    ids = np.where(rpn_match == 1)[0]
    ix = 0  # index into rpn_bbox
    # TODO: use box_refinment() rather than duplicating the code here
    for i, a in zip(ids, anchors[ids]):
        # Closest gt box (it might have IoU < 0.7)
        gt = gt_boxes[anchor_iou_argmax[i]]

        # Convert coordinates to center plus width/height.
        # GT Box
        gt_h = gt[2] - gt[0]
        gt_w = gt[3] - gt[1]
        gt_center_y = gt[0] + 0.5 * gt_h
        gt_center_x = gt[1] + 0.5 * gt_w
        # Anchor
        a_h = a[2] - a[0]
        a_w = a[3] - a[1]
        a_center_y = a[0] + 0.5 * a_h
        a_center_x = a[1] + 0.5 * a_w

        # Compute the bbox refinement that the RPN should predict.
        rpn_bbox[ix] = [
            (gt_center_y - a_center_y) / a_h,
            (gt_center_x - a_center_x) / a_w,
            np.log(gt_h / a_h),
            np.log(gt_w / a_w),
        ]
        # Normalize
        rpn_bbox[ix] /= Config.RPN.BBOX_STD_DEV
        ix += 1

    return rpn_match, rpn_bbox


class DataGenerator(Dataset):
    def __init__(self, dataset_handler, augmentation=None):
        """A generator that returns images and corresponding target class ids,
            bounding box deltas, and masks.

            dataset_handler: The Dataset object to pick data from
            config: The model config object
            shuffle: If True, shuffles the samples before every epoch
            augment: If True, applies image augmentation to images
                     (currently only horizontal flips are supported)

            Returns a Python generator. Upon calling next() on it, the
            generator returns two lists, inputs and outputs. The containtes
            of the lists differs depending on the received arguments:
            inputs list:
            - images: [batch, H, W, C]
            - image_metas: [batch, size of image meta]
            - rpn_match: [batch, N] Integer (1=positive anchor,
                                             -1=negative, 0=neutral)
            - rpn_bbox: [batch, N, (dy, dx, log(dh), log(dw))] Anchor bbox
                        deltas.
            - gt_class_ids: [batch, MAX_GT_INSTANCES] Integer class IDs
            - gt_boxes: [batch, MAX_GT_INSTANCES, (y1, x1, y2, x2)]
            - gt_masks: [batch, height, width, MAX_GT_INSTANCES]. The height
                        and width are those of the image unless use_mini_mask
                        is True, in which case they are defined in
                        MINI_MASK_SHAPE.

            outputs list: Usually empty in regular training. But if
                          detection_targets is True then the outputs list
                          contains target class_ids, bbox deltas, and masks.
            """
        self.b = 0  # batch item index
        self.image_index = -1
        self.image_ids = np.copy(dataset_handler.image_ids)
        self.error_count = 0

        self.dataset_handler = dataset_handler
        self.augmentation = augmentation

        # Anchors
        # [anchor_count, (y1, x1, y2, x2)]
        self.anchors = anchors.generate_pyramid_anchors(Config.RPN.ANCHOR.SCALES,
                                                        Config.RPN.ANCHOR.RATIOS,
                                                        Config.BACKBONE.SHAPES,
                                                        Config.BACKBONE.STRIDES,
                                                        Config.RPN.ANCHOR.STRIDE)
        self.anchors = torch.from_numpy(self.anchors)

    @profilable
    def __getitem__(self, image_index):
        # Get GT bounding boxes and masks for image.
        image_id = self.image_ids[image_index]
        while True:
            image, image_metas, gt_class_ids, gt_boxes, gt_masks = \
                load_image_gt(self.dataset_handler, image_id,
                              augmentation=self.augmentation,
                              use_mini_mask=Config.MINI_MASK.USE)
            if np.any(gt_class_ids > 0):
                break

        # RPN Targets
        rpn_match, rpn_bbox = build_rpn_targets(image.shape, self.anchors,
                                                gt_class_ids, gt_boxes)

        # If more instances than fits in the array, sub-sample from them.
        if gt_boxes.shape[0] > Config.DETECTION.MAX_GT_INSTANCES:
            ids = np.random.choice(np.arange(gt_boxes.shape[0]),
                                   Config.DETECTION.MAX_GT_INSTANCES,
                                   replace=False)
            gt_class_ids = gt_class_ids[ids]
            gt_boxes = gt_boxes[ids]
            gt_masks = gt_masks[:, :, ids]
        elif gt_boxes.shape[0] < Config.DETECTION.MAX_GT_INSTANCES:
            gt_class_ids_ = np.zeros((Config.DETECTION.MAX_GT_INSTANCES),
                                     dtype=np.int32)
            gt_class_ids_[:gt_class_ids.shape[0]] = gt_class_ids
            gt_class_ids = gt_class_ids_

            gt_boxes_ = np.zeros((Config.DETECTION.MAX_GT_INSTANCES, 4),
                                 dtype=np.int32)
            gt_boxes_[:gt_boxes.shape[0]] = gt_boxes
            gt_boxes = gt_boxes_

            gt_masks_ = np.zeros((gt_masks.shape[0], gt_masks.shape[1],
                                  Config.DETECTION.MAX_GT_INSTANCES),
                                 dtype=np.int32)
            gt_masks_[:, :, :gt_masks.shape[-1]] = gt_masks
            gt_masks = gt_masks_

        # Add to batch
        rpn_match = rpn_match[:, np.newaxis]
        image = utils.subtract_mean(image)

        # Convert to tensors
        image = torch.from_numpy(image.transpose(2, 0, 1)).float()
        rpn_match = torch.from_numpy(rpn_match)
        rpn_bbox = torch.from_numpy(rpn_bbox).float()
        gt_class_ids = torch.from_numpy(gt_class_ids)
        gt_boxes = torch.from_numpy(gt_boxes).float()
        gt_masks = torch.from_numpy(gt_masks.astype(int).transpose(2, 0, 1)).float()

        return (image, image_metas.to_numpy(), rpn_match, rpn_bbox,
                gt_class_ids, gt_boxes, gt_masks)

    def __len__(self):
        return self.image_ids.shape[0]
